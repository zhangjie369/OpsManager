"""
SSH 连接服务
封装 paramiko 实现 SSH 连接和命令执行
"""
import paramiko
import socket
from typing import Tuple, Optional


class SSHService:
    """SSH 服务类"""
    
    def __init__(self, host: str, port: int, username: str, 
                 password: str = None, key_path: str = None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_path = key_path
        self.client = None
    
    def connect(self) -> Tuple[bool, str]:
        """建立 SSH 连接"""
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            if self.key_path:
                # 使用密钥认证
                try:
                    key = paramiko.RSAKey.from_private_key_file(self.key_path)
                except paramiko.ssh_exception.PasswordRequiredException:
                    # 密钥有密码保护，使用 password 解密
                    key = paramiko.RSAKey.from_private_key_file(self.key_path, password=self.password)
                self.client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    pkey=key,
                    timeout=10
                )
            else:
                # 使用密码认证
                self.client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    timeout=10
                )
            
            return True, "连接成功"
            
        except paramiko.AuthenticationException:
            return False, "认证失败，请检查用户名和密码"
        except paramiko.SSHException as e:
            return False, f"SSH 连接错误: {str(e)}"
        except socket.timeout:
            return False, "连接超时，请检查服务器地址和端口"
        except socket.gaierror:
            return False, "无法解析服务器地址"
        except Exception as e:
            return False, f"连接失败: {str(e)}"
    
    def execute(self, command: str) -> Tuple[bool, str]:
        """执行远程命令"""
        if not self.client:
            return False, "未建立 SSH 连接"
        
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=30)
            exit_code = stdout.channel.recv_exit_status()
            
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
            
            if exit_code == 0:
                return True, output
            else:
                return False, error or output
                
        except Exception as e:
            return False, f"命令执行失败: {str(e)}"
    
    def read_file(self, filepath: str) -> Tuple[bool, str]:
        """读取远程文件"""
        if not self.client:
            return False, "未建立 SSH 连接"
        
        try:
            sftp = self.client.open_sftp()
            with sftp.file(filepath, 'r') as f:
                content = f.read().decode('utf-8')
            sftp.close()
            return True, content
        except FileNotFoundError:
            return False, f"文件不存在: {filepath}"
        except PermissionError:
            return False, f"权限不足: {filepath}"
        except Exception as e:
            return False, f"读取文件失败: {str(e)}"
    
    def write_file(self, filepath: str, content: str) -> Tuple[bool, str]:
        """写入远程文件"""
        if not self.client:
            return False, "未建立 SSH 连接"
        
        try:
            sftp = self.client.open_sftp()
            with sftp.file(filepath, 'w') as f:
                f.write(content.encode('utf-8'))
            sftp.close()
            return True, "文件写入成功"
        except PermissionError:
            return False, f"权限不足: {filepath}"
        except Exception as e:
            return False, f"写入文件失败: {str(e)}"
    
    def reload_nginx(self) -> Tuple[bool, str]:
        """重载 Nginx 配置"""
        success, msg = self.execute('systemctl reload nginx')
        if success:
            return success, msg
        return False, f"nginx 重载失败: {msg}"
    
    def test_nginx_config(self) -> Tuple[bool, str]:
        """测试 Nginx 配置"""
        # 尝试多个可能的 nginx 路径
        paths = ['/usr/local/nginx/sbin/nginx', '/usr/sbin/nginx', '/sbin/nginx', 'nginx']
        for path in paths:
            success, msg = self.execute(f'{path} -t')
            if success:
                return success, msg
        return False, "未找到 nginx 命令，请检查安装路径"
    
    def close(self):
        """关闭 SSH 连接"""
        if self.client:
            self.client.close()

    def execute_streaming(self, command: str):
        """
        流式执行远程命令（无超时，逐行 yield 输出）
        用于长时间运行命令的实时输出（如 kubectl scale + pod 轮询）
        Yields: (is_error: bool, line: str) 元组
        """
        if not self.client:
            yield (True, "错误: 未建立 SSH 连接")
            return

        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=None)
            # 合并 stdout 和 stderr 的输出
            import select
            channel = stdout.channel

            while not channel.closed and (channel.recv_ready() or channel.recv_stderr_ready() or not channel.exit_status_ready()):
                # 使用非阻塞读取避免阻塞
                import time
                if channel.recv_ready():
                    line = channel.recv(4096).decode('utf-8', errors='replace')
                    if line:
                        yield (False, line.rstrip('\n\r'))
                if channel.recv_stderr_ready():
                    err_line = channel.recv_stderr(4096).decode('utf-8', errors='replace')
                    if err_line:
                        yield (True, err_line.rstrip('\n\r'))
                time.sleep(0.05)

            # 读取剩余数据
            if channel.recv_ready():
                remaining = channel.recv(65536).decode('utf-8', errors='replace')
                if remaining:
                    for rline in remaining.splitlines():
                        if rline.strip():
                            yield (False, rline)
            if channel.recv_stderr_ready():
                err_remaining = channel.recv_stderr(65536).decode('utf-8', errors='replace')
                if err_remaining:
                    for eline in err_remaining.splitlines():
                        if eline.strip():
                            yield (True, eline)

            exit_code = channel.recv_exit_status() if channel.exit_status_ready() else -1
            if exit_code != 0:
                yield (True, f"[退出码: {exit_code}]")

        except Exception as e:
            yield (True, f"命令执行异常: {str(e)}")


def test_connection(host: str, port: int, username: str, 
                    password: str = None, key_path: str = None) -> Tuple[bool, str]:
    """测试 SSH 连接"""
    ssh = SSHService(host, port, username, password, key_path)
    success, message = ssh.connect()
    ssh.close()
    return success, message
