"""
Nginx 配置服务
解析和修改 nginx.conf 中的 upstream 配置
"""
import re
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Server:
    """服务器信息"""
    ip: str
    port: int
    weight: int = 1
    max_fails: int = 0
    fail_timeout: str = ''
    is_online: bool = True
    full_line: str = ''


@dataclass
class Upstream:
    """Upstream 集群信息"""
    name: str
    servers: List[Server]
    balance_type: str = 'round_robin'  # round_robin, ip_hash, least_conn


class NginxConfigParser:
    """Nginx 配置解析器"""
    
    def __init__(self, config_content: str):
        self.content = config_content
        self.upstreams = self._parse_upstreams()
    
    def _parse_upstreams(self) -> List[Upstream]:
        """解析所有 upstream 块"""
        upstreams = []
        debug_info = []
        
        # 逐个查找 upstream 块
        search_start = 0
        while True:
            # 查找 upstream 块开头
            match = re.search(r'upstream\s+(\S+)\s*\{', self.content[search_start:])
            if not match:
                break
            
            name = match.group(1)
            # 块内容开始位置（在大括号之后）
            content_start = search_start + match.end()
            
            # 查找匹配的闭合大括号
            brace_level = 1
            content_end = content_start
            for i in range(content_start, len(self.content)):
                if self.content[i] == '{':
                    brace_level += 1
                elif self.content[i] == '}':
                    brace_level -= 1
                    if brace_level == 0:
                        content_end = i
                        break
            
            block_content = self.content[content_start:content_end]
            
            # 判断负载均衡类型
            balance_type = 'round_robin'
            if 'ip_hash' in block_content:
                balance_type = 'ip_hash'
            elif 'least_conn' in block_content:
                balance_type = 'least_conn'
            
            # 解析服务器
            servers = self._parse_servers(block_content)
            
            # 调试信息
            debug_info.append({
                'name': name,
                'server_count': len(servers),
                'block_preview': block_content[:80].replace('\n', '\\n'),
                'block_hex': block_content[:100].encode('utf-8').hex()
            })
            
            if servers:
                upstreams.append(Upstream(name=name, servers=servers, balance_type=balance_type))
            
            # 移动到当前块之后继续搜索
            search_start = content_end + 1
        
        # 将调试信息存储到实例中
        self._debug_info = debug_info
        
        return upstreams
    
    def _parse_servers(self, block_content: str) -> List[Server]:
        """解析 upstream 块中的服务器（包含已注释的服务器）"""
        servers = []
        
        for line in block_content.split('\n'):
            original_line = line  # 保留原始行（包含缩进和可能的注释）
            line = line.strip()
            
            # 跳过空行
            if not line:
                continue
            
            # 判断是否是注释行（支持 #server 和 # server 两种格式）
            is_comment = line.startswith('#')
            
            # 去掉注释符号，获取干净的配置内容
            if is_comment:
                clean_content = line[1:].strip()
            else:
                clean_content = line
            
            # 跳过非 server 行
            if not clean_content.startswith('server'):
                continue
            
            # 解析 server 配置（full_line 保留原始缩进）
            server = self._parse_server_line(clean_content, original_line.rstrip('\r\n'))
            if server:
                servers.append(server)
        
        return servers
    
    def _parse_server_line(self, clean_line: str, full_line: str) -> Optional[Server]:
        """解析单行 server 配置
        clean_line: 去掉注释符号后的纯配置内容，如 'server 192.168.1.204;'
        full_line:  原始行（含缩进和可能的 # 注释），用于替换时的精确匹配
        """
        # 判断在线状态：原始行是否以 # 开头
        is_online = not full_line.lstrip().startswith('#')
        
        # 解析格式: server IP[:PORT] [weight=W] [max_fails=N] [fail_timeout=T];
        # 端口可选，默认 80
        # 先尝试匹配有端口格式: server IP:PORT [params];
        pattern_with_port = r'server\s+(\S+):(\d+)(.*?);'
        # 再尝试匹配无端口格式: server IP [params];
        pattern_no_port = r'server\s+(\S+)(.*?);'
        
        match = re.search(pattern_with_port, clean_line)
        if match:
            ip = match.group(1)
            port = int(match.group(2))
            params = match.group(3)
        else:
            match = re.search(pattern_no_port, clean_line)
            if match:
                ip = match.group(1)
                port = 80  # 默认端口 80
                params = match.group(2)
            else:
                return None
        
        # 解析参数
        weight = 1
        max_fails = 0
        fail_timeout = ''
        
        weight_match = re.search(r'weight=(\d+)', params)
        if weight_match:
            weight = int(weight_match.group(1))
        
        max_fails_match = re.search(r'max_fails=(\d+)', params)
        if max_fails_match:
            max_fails = int(max_fails_match.group(1))
        
        fail_timeout_match = re.search(r'fail_timeout=(\S+)', params)
        if fail_timeout_match:
            fail_timeout = fail_timeout_match.group(1)
        
        return Server(
            ip=ip,
            port=port,
            weight=weight,
            max_fails=max_fails,
            fail_timeout=fail_timeout,
            is_online=is_online,
            full_line=full_line
        )
    
    def get_upstream(self, name: str) -> Optional[Upstream]:
        """获取指定 upstream"""
        for upstream in self.upstreams:
            if upstream.name == name:
                return upstream
        return None
    
    def get_all_upstreams(self) -> List[Dict]:
        """获取所有 upstream 信息"""
        result = []
        for upstream in self.upstreams:
            servers = []
            for server in upstream.servers:
                servers.append({
                    'ip': server.ip,
                    'port': server.port,
                    'weight': server.weight,
                    'max_fails': server.max_fails,
                    'is_online': server.is_online
                })
            
            online_count = sum(1 for s in upstream.servers if s.is_online)
            total_count = len(upstream.servers)
            
            result.append({
                'name': upstream.name,
                'balance_type': upstream.balance_type,
                'servers': servers,
                'online_count': online_count,
                'total_count': total_count,
                'health_percent': int(online_count / total_count * 100) if total_count > 0 else 0
            })
        
        return result
    
    def get_merged_server_groups(self) -> List[Dict]:
        """获取合并后的服务器组（相同 IP 的 upstream 合并显示）"""
        # 按 IP+端口 分组，记录每个 IP 属于哪些 upstream
        server_groups = {}  # key: "ip:port", value: {ip, port, upstreams: [{name, balance_type}...], is_online}
        
        for upstream in self.upstreams:
            for server in upstream.servers:
                key = f"{server.ip}:{server.port}"
                
                if key not in server_groups:
                    server_groups[key] = {
                        'ip': server.ip,
                        'port': server.port,
                        'upstreams': [],
                        'is_online': server.is_online
                    }
                
                # 记录该服务器属于哪些 upstream（包含负载均衡类型）
                upstream_info = {
                    'name': upstream.name,
                    'balance_type': upstream.balance_type
                }
                # 避免重复添加
                if not any(u['name'] == upstream.name for u in server_groups[key]['upstreams']):
                    server_groups[key]['upstreams'].append(upstream_info)
        
        # 转换为列表
        groups = list(server_groups.values())
        
        # 计算在线状态：只要该 IP 在任意一个 upstream 中在线，就认为在线
        for group in groups:
            group['is_online'] = any(
                server.is_online
                for upstream in self.upstreams
                for server in upstream.servers
                if server.ip == group['ip'] and server.port == group['port']
            )
        
        return groups


class NginxConfigModifier:
    """Nginx 配置修改器"""
    
    def __init__(self, config_content: str):
        self.content = config_content
        self.parser = NginxConfigParser(config_content)
    
    def set_server_status(self, upstream_name: str, server_ip: str, 
                          server_port: int, online: bool) -> Tuple[bool, str]:
        """设置服务器上线/下线状态（支持合并模式，同时修改所有包含该 IP 的 upstream）"""
        upstream = self.parser.get_upstream(upstream_name)
        if not upstream:
            return False, f"未找到 upstream: {upstream_name}"
        
        # 查找服务器
        server = None
        for s in upstream.servers:
            if s.ip == server_ip and s.port == server_port:
                server = s
                break
        
        if not server:
            return False, f"未找到服务器: {server_ip}:{server_port}"
        
        # 构建新的配置行
        new_line = self._build_server_line(server, online)

        # 替换配置（使用原始行进行替换，支持同时修改多个 upstream）
        old_line = server.full_line
        if old_line == new_line:
            current_state = "在线" if server.is_online else "离线"
            # 幂等操作：目标状态与当前状态一致，视为成功
            return True, f"{server_ip}:{server_port} 已是{current_state}状态，无需更改"

        self.content = self.content.replace(old_line, new_line)
        return True, f"已将 {server_ip}:{server_port} {'上线' if online else '下线'}"
    
    def set_server_status_batch(self, upstream_names: List[str], server_ip: str,
                                 server_port: int, online: bool) -> Tuple[bool, str]:
        """批量设置服务器状态（同时修改所有指定的 upstream）"""
        modified_count = 0
        modified_upstreams = []
        
        for upstream_name in upstream_names:
            upstream = self.parser.get_upstream(upstream_name)
            if not upstream:
                continue
            
            # 查找服务器
            server = None
            for s in upstream.servers:
                if s.ip == server_ip and s.port == server_port:
                    server = s
                    break
            
            if not server:
                continue
            
            # 构建新的配置行
            new_line = self._build_server_line(server, online)
            
            # 替换配置（使用正则表达式，更灵活地匹配带缩进的行）
            old_line = server.full_line
            if old_line != new_line:
                # 使用正则替换，支持任意缩进
                escaped_old = re.escape(old_line)
                pattern = r'^\s*' + escaped_old + r'$'
                self.content = re.sub(pattern, new_line, self.content, flags=re.MULTILINE)
                modified_count += 1
                modified_upstreams.append(upstream_name)
        
        # 重新解析配置以更新 server.full_line 等信息
        if modified_count > 0:
            self.parser = NginxConfigParser(self.content)
        
        if modified_count == 0:
            # 幂等操作：所有 upstream 中该服务器都已是目标状态，视为成功
            current_state = "在线" if online else "离线"
            return True, f"{server_ip}:{server_port} 所有集群中已为{current_state}状态"
        
        return True, f"已将 {server_ip}:{server_port} {'上线' if online else '下线'} (已同步到 {len(modified_upstreams)} 个集群: {', '.join(modified_upstreams)})"
    
    def _build_server_line(self, server: Server, online: bool) -> str:
        """
        基于原始行 full_line 切换注释状态，保留原始缩进和格式。
        full_line 示例（含缩进）：
          - 在线: '        server 192.168.1.204;'
          - 离线: '        #server 192.168.1.204;'
        """
        full = server.full_line
        
        if online:
            # 上线：去掉行首缩进后的 # 及紧跟的空格（支持 #server 和 # server 两种格式）
            new_line = re.sub(r'^(\s*)#\s?', r'\1', full)
        else:
            # 离线：仅在未注释时插入 '#'，避免重复 ## 
            if not full.lstrip().startswith('#'):
                new_line = re.sub(r'^(\s*)', r'\1#', full)
            else:
                new_line = full
        
        return new_line
    
    def get_content(self) -> str:
        """获取修改后的配置"""
        return self.content


def create_operation_log(action: str, upstream: str, server_ip: str, 
                        port: int, success: bool, message: str) -> Dict:
    """创建操作日志"""
    return {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,
        'upstream': upstream,
        'server': f'{server_ip}:{port}',
        'success': success,
        'message': message
    }
