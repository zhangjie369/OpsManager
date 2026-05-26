"""
LVS Keepalived 管理服务
封装 LVS 状态查询、RS 上下线、RS 互换等操作
基于 SSH 远程执行 ipvsadm/sed/systemctl 命令
"""
from typing import Tuple, Union, List, Dict
from datetime import datetime


class LVSService:
    """LVS Keepalived 管理服务类"""

    def __init__(self, ssh, ip_prefix: str = '192.168.1.',
                 conf_dir: str = '/etc/keepalived'):
        """
        初始化 LVS 服务

        Args:
            ssh: 已连接的 SSHService 实例
            ip_prefix: IP 地址前缀，如 '192.168.1.'
            conf_dir: keepalived 配置根目录，默认 /etc/keepalived
        """
        self.ssh = ssh
        self.ip_prefix = ip_prefix
        self.conf_dir = conf_dir

        # 配置文件路径（对应 bash 脚本中的变量）
        self.vs_file_pattern = f'{conf_dir}/conf.d/virtual_server/vs_*.conf'
        self.rs_file_pattern = f'{conf_dir}/conf.d/real_server/rs_*.conf'
        self.vs_match_string = 'vs_'
        self.rs_match_string = 'rs_'

    def get_lvs_status(self) -> Tuple[bool, Union[str, Dict]]:
        """
        获取 LVS 完整状态（ipvsadm 实时状态 + keepalived 配置拓扑合并）

        Returns:
            成功时返回 (True, {virtual_servers: [...]})
            失败时返回 (False, 错误信息)
        """
        # 1. 执行 ipvsadm -ln 获取实时转发规则
        success, output = self.ssh.execute('ipvsadm -ln')
        if not success:
            return False, f'执行 ipvsadm -ln 失败: {output}'

        # 解析 ipvsadm 输出，提取 VS 和 RS 关系
        virtual_servers = self._parse_ipvsadm(output)

        # 2. 获取 keepalived 配置拓扑（可选补充信息）
        success, include_output = self.ssh.execute(
            f"grep 'include' {self.vs_file_pattern}"
        )
        if success:
            self._enrich_with_config_topology(virtual_servers, include_output)

        return True, {'virtual_servers': virtual_servers}

    def _parse_ipvsadm(self, output: str) -> List[Dict]:
        """
        解析 ipvsadm -ln 的输出

        输出格式示例：
          IP Virtual Server version 1.2.1 (size=4096)
          Prot LocalAddress:Port Scheduler Flags
            -> RemoteAddress:Port           Forward Weight ActiveConn InActConn
          TCP  192.168.1.207:80 rr persistent 50
            -> 192.168.1.215:80           Route   1      0          0
            -> 192.168.1.209:80           Route   1      0          0
        """
        virtual_servers = []
        current_vs = None

        lines = output.strip().split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 跳过标题行
            if line.startswith('IP Virtual Server') or 'Prot' in line or 'Remote' in line:
                continue

            # VS 行：TCP/UDP VIP:PORT scheduler [flags]
            if line.startswith('TCP') or line.startswith('UDP'):
                parts = line.split()
                if len(parts) >= 3:
                    protocol = parts[0]
                    vip_port = parts[1]
                    vip, port = self._split_ip_port(vip_port)

                    if current_vs:
                        virtual_servers.append(current_vs)

                    current_vs = {
                        'vip': vip,
                        'port': port,
                        'protocol': protocol,
                        'scheduler': parts[2] if len(parts) > 2 else 'rr',
                        'rss': []
                    }
            elif line.startswith('->') and current_vs:
                # RS 行：-> RIP:Port Forward Weight ActiveConn InActConn
                parts = line.split()[1:]  # 去掉 ->
                if len(parts) >= 5:
                    rip_port = parts[0]
                    rip, rport = self._split_ip_port(rip_port)

                    rs_info = {
                        'ip': rip,
                        'port': rport,
                        'forward': parts[1] if len(parts) > 1 else 'Route',
                        'weight': int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
                        'online': True  # 出现在 ipvsadm 中即表示在线
                    }
                    current_vs['rss'].append(rs_info)

        # 添加最后一个 VS
        if current_vs:
            virtual_servers.append(current_vs)

        return virtual_servers

    def _split_ip_port(self, addr: str) -> tuple:
        """将 IP:PORT 拆分为 (ip, port)"""
        if ':' in addr:
            parts = addr.rsplit(':', 1)
            return parts[0], int(parts[1])
        return addr, 0

    def _enrich_with_config_topology(self, virtual_servers: List[Dict],
                                      include_output: str):
        """用 keepalived 配置拓扑补充数据（标记离线 RS 等）"""
        # 收集所有已在线 RS 的 key
        online_rs_keys = set()
        for vs in virtual_servers:
            for rs in vs['rss']:
                online_rs_keys.add(f"{rs['ip']}:{rs['port']}")

        # 从 keepalived 配置中找出离线 RS（配置存在但不在 ipvsadm 中）
        # 这里可以扩展解析 conf.d 目录获取完整的 RS 列表

    def set_rs_status(self, vs_ip_suffix: str, rs_ip_suffix: str,
                       state: str) -> Tuple[bool, str]:
        """
        对指定 VS 下的某个 RS 执行上线(on)/下线(off) 操作

        Args:
            vs_ip_suffix: VS IP 后缀（如 '207' 代表 192.168.1.207）
            rs_ip_suffix: RS IP 后缀（如 '215' 代表 192.168.1.215）
            state: 'on'(上线) 或 'off'(下线)

        Returns:
            (是否成功, 消息)
        """
        vs_full_ip = f'{self.ip_prefix}{vs_ip_suffix}'
        rs_full_ip = f'{self.ip_prefix}{rs_ip_suffix}'

        # 1. 校验参数格式
        if not (vs_ip_suffix.isdigit() and rs_ip_suffix.isdigit()):
            return False, 'IP 后缀必须是数字 (1-254)'

        # 2. 定位匹配的 VS 配置文件列表
        list_cmd = f"ls {self.vs_file_pattern} | grep {self.vs_match_string}{vs_full_ip}"
        success, file_list = self.ssh.execute(list_cmd)

        if not success or not file_list.strip():
            return False, f'未找到 VS {vs_full_ip} 的配置文件'

        vs_files = file_list.strip().split('\n')

        # 3. 定位匹配的 RS 文件（用于校验 RS 是否存在）
        check_cmd = f"ls {self.rs_file_pattern} | grep {self.rs_match_string}{rs_full_ip}"
        success, rs_check = self.ssh.execute(check_cmd)

        if not success or not rs_check.strip():
            return False, f'未找到 RS {rs_full_ip} 的配置文件'

        # 4. 对每个 VS 文件执行 sed 操作
        for vs_file in vs_files:
            vs_file = vs_file.strip()
            if not vs_file:
                continue

            if state == 'on':
                # 上线：取消注释（移除行首的 ! 和 #）
                sed_cmd = f"sed -i '/{self.rs_match_string}{rs_full_ip}/{{s/[!#]//g}}' {vs_file}"
            elif state == 'off':
                # 下线：在行首加 !
                sed_cmd = f"sed -i '/{self.rs_match_string}{rs_full_ip}/{{s/^/!/g}}' {vs_file}"
            else:
                return False, f'无效的状态值: {state}, 必须是 on 或 off'

            success, msg = self.ssh.execute(sed_cmd)
            if not success:
                return False, f'modify {vs_file} failed: {msg}'

            # 验证修改结果
            verify_cmd = f"grep '{self.rs_match_string}{rs_full_ip}' {vs_file}"
            success, verify_result = self.ssh.execute(verify_cmd)
            action_text = 'cancel comment' if state == 'on' else 'comment'
            if success:
                pass  # 验证通过

        # 5. reload keepalived
        reload_success, reload_msg = self.reload_keepalived()
        if not reload_success:
            return False, f'reload keepalived failed: {reload_msg}'

        action_text = '上线' if state == 'on' else '下线'
        return True, f'RS {rs_full_ip} 已{action_text}(VS: {vs_full_ip})'

    def swap_rs(self, vs_ip_suffix: str, rs_ip1_suffix: str,
                rs_ip2_suffix: str) -> Tuple[bool, str]:
        """
        互换同一 VS 下两个 RS 的在线/离线状态

        前置校验：两个 RS 必须一上一下才能互换

        Args:
            vs_ip_suffix: VS IP 后缀
            rs_ip1_suffix: 第一个 RS IP 后缀
            rs_ip2_suffix: 第二个 RS IP 后缀
        """
        vs_full_ip = f'{self.ip_prefix}{vs_ip_suffix}'
        rs1_full_ip = f'{self.ip_prefix}{rs_ip1_suffix}'
        rs2_full_ip = f'{self.ip_prefix}{rs_ip2_suffix}'

        # 1. 参数校验
        if not all([vs_ip_suffix.isdigit(), rs_ip1_suffix.isdigit(),
                     rs_ip2_suffix.isdigit()]):
            return False, 'IP 后缀必须是数字 (1-254)'

        if rs_ip1_suffix == rs_ip2_suffix:
            return False, 'RS1 与 RS2 不能相同'

        # 2. 通过 ipvsadm 确认两 RS 当前状态
        success, status_data = self.get_lvs_status()
        if not success:
            return False, f'获取 LVS 状态失败: {status_data}'

        target_vs = None
        for vs in status_data.get('virtual_servers', []):
            if vs['vip'] == vs_full_ip:
                target_vs = vs
                break

        if not target_vs:
            return False, f'未找到 VS {vs_full_ip}'

        # 提取两 RS 在当前 VS 下的在线状态
        rs1_online = None
        rs2_online = None
        for rs in target_vs['rss']:
            if rs['ip'] == rs1_full_ip:
                rs1_online = True
            if rs['ip'] == rs2_full_ip:
                rs2_online = True

        # 如果 RS 不在 ipvsadm 中出现说明它被注释了（离线）
        if rs1_online is None:
            rs1_online = False
        if rs2_online is None:
            rs2_online = False

        # 3. 安全校验：必须一上一下
        if rs1_online and rs2_online:
            return False, f'{rs1_full_ip} 和 {rs2_full_ip} 都在线，无法互换'
        if not rs1_online and not rs2_online:
            return False, f'{rs1_full_ip} 和 {rs2_full_ip} 都离线，无法互换'

        # 4. 执行互换操作
        # 在线的那个 → 下线，离线的那个 → 上线
        if rs1_online:
            success, msg = self.set_rs_status(vs_ip_suffix, rs_ip1_suffix, 'off')
            if not success:
                return False, f'下线 {rs1_full_ip} 失败: {msg}'
            success, msg = self.set_rs_status(vs_ip_suffix, rs_ip2_suffix, 'on')
            if not success:
                return False, f'上线 {rs2_full_ip} 失败: {msg}'
        else:
            success, msg = self.set_rs_status(vs_ip_suffix, rs_ip1_suffix, 'on')
            if not success:
                return False, f'上线 {rs1_full_ip} 失败: {msg}'
            success, msg = self.set_rs_status(vs_ip_suffix, rs_ip2_suffix, 'off')
            if not success:
                return False, f'下线 {rs2_full_ip} 失败: {msg}'

        return True, f'Swap 成功: {rs1_full_ip} <-> {rs2_full_ip} (VS: {vs_full_ip})'

    def get_keepalived_status(self) -> Tuple[bool, Union[str, str]]:
        """
        查看 keepalived 配置文件中 VS-RS include 关系（对应 bash status 命令）
        """
        success, output = self.ssh.execute(
            f"grep 'include' {self.vs_file_pattern}"
        )
        if not success:
            return False, f'读取 keepalived 配置失败: {output}'
        return True, output

    def reload_keepalived(self) -> Tuple[bool, str]:
        """重载 keepalived 配置"""
        success, output = self.ssh.execute('systemctl reload keepalived')
        if success:
            return True, 'keepalived reloaded'
        return False, f'reload failed: {output}'


def create_lvs_operation_log(action: str, vs_ip: str, rs_ip: str,
                              success: bool, message: str) -> Dict:
    """创建 LVS 操作日志记录"""
    return {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,       # 'op_on', 'op_off', 'swap'
        'module': 'lvs',
        'vs_ip': vs_ip,
        'rs_ip': rs_ip,
        'success': success,
        'message': message
    }
