"""
配置管理模块
支持 SSH 连接信息配置和密码加密存储
"""
import os
import json
from cryptography.fernet import Fernet

# 加密密钥 - 首次运行自动生成
KEY_FILE = os.path.join(os.path.dirname(__file__), '.key')
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'servers.json')
LVS_CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'lvs_servers.json')
ARGOCD_CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'argocd_servers.json')


def get_fernet():
    """获取 Fernet 加密实例"""
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
    
    with open(KEY_FILE, 'rb') as f:
        key = f.read()
    return Fernet(key)


def encrypt_password(password):
    """加密密码"""
    fernet = get_fernet()
    return fernet.encrypt(password.encode()).decode()


def decrypt_password(encrypted):
    """解密密码"""
    fernet = get_fernet()
    return fernet.decrypt(encrypted.encode()).decode()


def _migrate_server_format(server):
    """兼容旧格式：将 nginx_conf 迁移为 nginx_confs"""
    if 'nginx_conf' in server and 'nginx_confs' not in server:
        old_conf = server.pop('nginx_conf')
        if old_conf:
            server['nginx_confs'] = [{"name": "默认配置", "path": old_conf}]
        else:
            server['nginx_confs'] = []
    # 确保 nginx_confs 存在
    if 'nginx_confs' not in server:
        server['nginx_confs'] = []
    return server


def load_servers():
    """加载服务器配置"""
    if not os.path.exists(CONFIG_FILE):
        return []
    
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 解密密码
    fernet = get_fernet()
    for server in data:
        if 'password' in server and server['password']:
            try:
                server['password'] = fernet.decrypt(server['password'].encode()).decode()
            except:
                pass
        # 迁移旧格式
        _migrate_server_format(server)
    
    return data


def save_servers(servers):
    """保存服务器配置"""
    # 加密密码
    fernet = get_fernet()
    data = []
    for server in servers:
        s = server.copy()
        if 'password' in s and s['password']:
            s['password'] = fernet.encrypt(s['password'].encode()).decode()
        data.append(s)
    
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


# ========== LVS 服务器配置（独立于 Nginx）==========

def load_lvs_servers():
    """加载 LVS 服务器配置（独立配置文件 lvs_servers.json）"""
    if not os.path.exists(LVS_CONFIG_FILE):
        return []
    
    with open(LVS_CONFIG_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 解密密码
    fernet = get_fernet()
    for server in data:
        if 'password' in server and server['password']:
            try:
                server['password'] = fernet.decrypt(server['password'].encode()).decode()
            except:
                pass
    
    return data


def save_lvs_servers(servers):
    """保存 LVS 服务器配置"""
    fernet = get_fernet()
    data = []
    for server in servers:
        s = server.copy()
        if 'password' in s and s['password']:
            s['password'] = fernet.encrypt(s['password'].encode()).decode()
        data.append(s)
    
    with open(LVS_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


# ========== ArgoCD 服务器配置（完全独立）==========

def load_argocd_servers():
    """加载 ArgoCD 服务器（K8s 集群节点）配置"""
    if not os.path.exists(ARGOCD_CONFIG_FILE):
        return []
    
    with open(ARGOCD_CONFIG_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 解密密码
    fernet = get_fernet()
    for server in data:
        if 'password' in server and server['password']:
            try:
                server['password'] = fernet.decrypt(server['password'].encode()).decode()
            except:
                pass
    
    return data


def save_argocd_servers(servers):
    """保存 ArgoCD 服务器配置"""
    fernet = get_fernet()
    data = []
    for server in servers:
        s = server.copy()
        if 'password' in s and s['password']:
            s['password'] = fernet.encrypt(s['password'].encode()).decode()
        data.append(s)
    
    with open(ARGOCD_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


# 默认 Nginx 服务器配置
DEFAULT_SERVERS = [
    {
        'id': 1,
        'name': 'Nginx 服务器',
        'host': 'your-server-ip',
        'port': 22,
        'username': 'root',
        'password': '',  # 请修改为实际密码
        'key_path': '',
        'nginx_confs': [
            {"name": "默认配置", "path": "/usr/local/nginx/conf/conf.d/upstreamserver/upstreamserver_iis.conf"}
        ]
    }
]

# 默认 LVS 服务器配置
DEFAULT_LVS_SERVERS = [
    {
        'id': 1,
        'name': 'LVS 主节点',
        'host': 'your-server-ip',       # Keepalived 所在主机
        'port': 22,
        'username': 'root',
        'password': '',                  # 请修改为实际密码
        'key_path': '',
        'ip_prefix': '192.168.1.',      # LVS IP 前缀
        'conf_dir': '/etc/keepalived'    # keepalived 配置根目录
    }
]

# 默认 ArgoCD 服务器配置（K8s 集群管理节点）
DEFAULT_ARGOCD_SERVERS = [
    {
        'id': 1,
        'name': 'K8s 管理节点',
        'host': 'your-server-ip',       # 有 kubectl 权限的 K8s 节点
        'port': 22,
        'username': 'root',
        'password': '',                  # 请修改为实际密码
        'key_path': ''
    }
]




# Flask 配置
SECRET_KEY = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')
DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', 5000))
