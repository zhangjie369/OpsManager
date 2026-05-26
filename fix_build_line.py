import re

filepath = "services/nginx_service.py"
with open(filepath, encoding="utf-8") as f:
    content = f.read()

old = '''    def _build_server_line(self, server: Server, online: bool) -> str:
        """构建 server 配置行"""
        prefix = '' if online else '# '
        
        # 端口为 80 时不输出端口，保持简洁
        port_str = f':{server.port}' if server.port != 80 else ''
        
        params = f' weight={server.weight}' if server.weight > 1 else ''
        params += f' max_fails={server.max_fails}' if server.max_fails > 0 else ''
        params += f' fail_timeout={server.fail_timeout}' if server.fail_timeout else ''
        
        return f'{prefix}server {server.ip}{port_str}{params};'''

new = '''    def _build_server_line(self, server: Server, online: bool) -> str:
        """
        基于原始行 full_line 切换注释状态，保留原始缩进和格式。
        full_line 示例（含缩进）：
          - 在线: '        server your-server-ip;'
          - 离线: '        # server your-server-ip;'
        """
        full = server.full_line
        
        if online:
            # 上线：去掉行首缩进后的 # 及紧跟的空格（支持 #server 和 # server 两种格式）
            new_line = re.sub(r'^(\\s*)#\\s?', r'\\1', full)
        else:
            # 离线：在第一个非空白字符前插入 '# '
            new_line = re.sub(r'^(\\s*)', r'\\1# ', full)
        
        return new_line'''

if old in content:
    content = content.replace(old, new)
    with open(filepath, 'w', encoding="utf-8") as f:
        f.write(content)
    print("OK - 已替换 _build_server_line")
else:
    print("NOT FOUND - 未找到匹配内容，请手动检查")
    idx = content.find("_build_server_line")
    if idx >= 0:
        print("附近内容:", repr(content[idx:idx+150]))
