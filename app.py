"""
Flask Web 应用
Nginx upstream 管理平台主入口
"""
import os
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from config import (
    SECRET_KEY, DEBUG, HOST, PORT,
    load_servers, save_servers, DEFAULT_SERVERS, encrypt_password,
    load_lvs_servers, save_lvs_servers, DEFAULT_LVS_SERVERS,
    load_argocd_servers, save_argocd_servers, DEFAULT_ARGOCD_SERVERS
)
from services import SSHService, test_connection
from services import NginxConfigParser, NginxConfigModifier, create_operation_log
from services import LVSService, create_lvs_operation_log
from services import ArgoCDService, create_argocd_operation_log
from services import ScaleService, create_scale_operation_log

app = Flask(__name__)
app.secret_key = SECRET_KEY


# 操作日志存储（启动时从文件加载，见下方）

# 错误日志文件路径
ERROR_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
ERROR_LOG_FILE = os.path.join(ERROR_LOG_DIR, 'error.log')
# 操作日志文件路径（JSONL格式，每行一条JSON）
OPERATION_LOG_FILE = os.path.join(ERROR_LOG_DIR, 'operation.log')
LVS_OPERATION_LOG_FILE = os.path.join(ERROR_LOG_DIR, 'lvs_operation.log')
ARGOCD_OPERATION_LOG_FILE = os.path.join(ERROR_LOG_DIR, 'argocd_operation.log')
SCALE_OPERATION_LOG_FILE = os.path.join(ERROR_LOG_DIR, 'scale_operation.log')


def _write_log_to_file(log_entry: dict, filepath: str):
    """追加单条日志到 JSONL 文件"""
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    except Exception:
        pass  # 文件写入失败不影响主流程


def _load_logs_from_file(filepath: str, limit: int = 100) -> list:
    """从 JSONL 文件加载最近的操作日志（按时间倒序）"""
    logs = []
    if not os.path.isfile(filepath):
        return logs
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        logs.append(json.loads(line))
                    except (json.JSONDecodeError, TypeError):
                        continue
    except Exception:
        return logs
    # 按时间倒序，取前 N 条
    logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return logs[:limit]

# 当前配置缓存
current_config = {}

# 启动时从文件加载历史操作日志（重启不丢失）
operation_logs = _load_logs_from_file(OPERATION_LOG_FILE)
lvs_operation_logs = _load_logs_from_file(LVS_OPERATION_LOG_FILE)
argocd_operation_logs = _load_logs_from_file(ARGOCD_OPERATION_LOG_FILE)
scale_operation_logs = _load_logs_from_file(SCALE_OPERATION_LOG_FILE)


def get_server_ssh(server_config):
    """创建 SSH 连接（Nginx 服务器）"""
    return SSHService(
        host=server_config['host'],
        port=server_config.get('port', 22),
        username=server_config['username'],
        password=server_config.get('password'),
        key_path=server_config.get('key_path')
    )


def get_lvs_server_ssh(lvs_config):
    """创建 SSH 连接（LVS/Keepalived 服务器）"""
    return SSHService(
        host=lvs_config['host'],
        port=lvs_config.get('port', 22),
        username=lvs_config['username'],
        password=lvs_config.get('password'),
        key_path=lvs_config.get('key_path')
    )


def get_lvs_service(lvs_config):
    """创建 LVSService 实例"""
    ssh = get_lvs_server_ssh(lvs_config)
    return LVSService(
        ssh=ssh,
        ip_prefix=lvs_config.get('ip_prefix', '192.168.13.'),
        conf_dir=lvs_config.get('conf_dir', '/etc/keepalived')
    )


def get_argocd_service(argocd_config):
    """创建 ArgoCDService 实例并建立 SSH 连接"""
    ssh = SSHService(
        host=argocd_config['host'],
        port=argocd_config.get('port', 22),
        username=argocd_config['username'],
        password=argocd_config.get('password'),
        key_path=argocd_config.get('key_path')
    )
    ok, msg = ssh.connect()
    if not ok:
        raise Exception(f'ArgoCD 服务器 SSH 连接失败: {msg}')
    return ArgoCDService(ssh=ssh)


@app.route('/')
def index():
    """主页"""
    return render_template('index.html')


@app.route('/api/servers', methods=['GET'])
def get_servers():
    """获取服务器列表"""
    servers = load_servers()
    return jsonify({'success': True, 'data': servers})


@app.route('/api/servers', methods=['POST'])
def add_server():
    """添加服务器"""
    data = request.json
    servers = load_servers()
    
    # 生成新 ID
    new_id = max([s.get('id', 0) for s in servers], default=0) + 1
    data['id'] = new_id
    
    servers.append(data)
    save_servers(servers)
    
    return jsonify({'success': True, 'message': '服务器添加成功', 'id': new_id})


@app.route('/api/servers/<int:server_id>', methods=['PUT'])
def update_server(server_id):
    """更新服务器"""
    data = request.json
    servers = load_servers()
    
    for i, s in enumerate(servers):
        if s.get('id') == server_id:
            data['id'] = server_id
            servers[i] = data
            save_servers(servers)
            return jsonify({'success': True, 'message': '服务器更新成功'})
    
    return jsonify({'success': False, 'message': '服务器不存在'})


@app.route('/api/servers/<int:server_id>', methods=['DELETE'])
def delete_server(server_id):
    """删除服务器"""
    servers = load_servers()
    servers = [s for s in servers if s.get('id') != server_id]
    save_servers(servers)
    return jsonify({'success': True, 'message': '服务器删除成功'})


@app.route('/api/servers/<int:server_id>/test', methods=['POST'])
def test_server_connection(server_id):
    """测试服务器连接"""
    servers = load_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)
    
    if not server:
        return jsonify({'success': False, 'message': '服务器不存在'})
    
    success, message = test_connection(
        host=server['host'],
        port=server.get('port', 22),
        username=server['username'],
        password=server.get('password'),
        key_path=server.get('key_path')
    )
    
    return jsonify({'success': success, 'message': message})


@app.route('/api/config/<int:server_id>', methods=['GET'])
def get_config(server_id):
    """获取 Nginx 配置"""
    servers = load_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)
    
    if not server:
        return jsonify({'success': False, 'message': '服务器不存在'})
    
    # 使用 SSH 读取配置文件
    ssh = get_server_ssh(server)
    success, message = ssh.connect()
    
    if not success:
        return jsonify({'success': False, 'message': f'SSH 连接失败: {message}'})
    
    config_path = server.get('nginx_conf', '/etc/nginx/nginx.conf')
    success, content = ssh.read_file(config_path)
    
    if not success:
        ssh.close()
        return jsonify({'success': False, 'message': f'读取配置失败: {content}'})
    
    # 缓存配置
    current_config[server_id] = content
    
    # 解析配置
    parser = NginxConfigParser(content)
    upstreams = parser.get_all_upstreams()
    
    # 获取合并后的服务器组（相同 IP 的 upstream 合并显示）
    server_groups = parser.get_merged_server_groups()
    
    ssh.close()
    
    # 统计 upstream 关键字数量（用于调试）
    upstream_count = content.lower().count('upstream')
    
    return jsonify({
        'success': True,
        'upstreams': upstreams,
        'server_groups': server_groups,  # 合并后的服务器组
        'config_path': config_path,
        'debug_info': {
            'file_size': len(content),
            'upstream_keyword_count': upstream_count,
            'parsed_upstream_count': len(upstreams),
            'server_group_count': len(server_groups),
            'parse_details': getattr(parser, '_debug_info', []),
            'raw_content': content
        }
    })


@app.route('/api/config/<int:server_id>/raw', methods=['GET'])
def get_config_raw(server_id):
    """获取 Nginx 原始配置内容（用于预览）"""
    servers = load_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)
    
    if not server:
        return jsonify({'success': False, 'message': '服务器不存在'})
    
    # 使用 SSH 读取配置文件
    ssh = get_server_ssh(server)
    success, message = ssh.connect()
    
    if not success:
        return jsonify({'success': False, 'message': f'SSH 连接失败: {message}'})
    
    config_path = server.get('nginx_conf', '/etc/nginx/nginx.conf')
    success, content = ssh.read_file(config_path)
    
    if not success:
        ssh.close()
        return jsonify({'success': False, 'message': f'读取配置失败: {content}'})
    
    ssh.close()
    
    return jsonify({
        'success': True,
        'config_path': config_path,
        'content': content
    })


@app.route('/api/server/<int:server_id>/action', methods=['POST'])
def server_action(server_id):
    """服务器上线/下线操作（支持批量操作多个 upstream）"""
    data = request.json
    upstream_names = data.get('upstreams')  # 支持多个 upstream 名称的列表（可能是字符串或字典）
    # 统一提取为字符串列表（兼容前端传 dict 或 string）
    if upstream_names and isinstance(upstream_names[0], dict):
        upstream_names = [u.get('name', '') for u in upstream_names]
    server_ip = data.get('ip')
    server_port = data.get('port')
    action = data.get('action')  # 'up' or 'down'
    
    servers = load_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)
    
    if not server:
        return jsonify({'success': False, 'message': '服务器不存在'})
    
    # 获取缓存的配置或重新读取
    if server_id not in current_config:
        ssh = get_server_ssh(server)
        success, message = ssh.connect()
        
        if not success:
            return jsonify({'success': False, 'message': f'SSH 连接失败: {message}'})
        
        config_path = server.get('nginx_conf', '/etc/nginx/nginx.conf')
        success, content = ssh.read_file(config_path)
        
        if not success:
            ssh.close()
            return jsonify({'success': False, 'message': f'读取配置失败: {content}'})
        
        current_config[server_id] = content
        ssh.close()
    
    # 修改配置（支持批量操作）
    modifier = NginxConfigModifier(current_config[server_id])
    online = (action == 'up')

    if not upstream_names:
        return jsonify({'success': False, 'message': '未指定 upstream'})

    # 下线前置校验：同一 upstream 集群至少保留 1 台在线（可通过 force 强制跳过）
    force = data.get('force', False)
    if not online and not force:
        parser = modifier.parser
        blocked_upstreams = []
        for u_name in upstream_names:
            upstream = parser.get_upstream(u_name)
            if upstream:
                online_count = sum(1 for s in upstream.servers if s.is_online)
                if online_count <= 1:
                    blocked_upstreams.append(u_name)
        if blocked_upstreams:
            return jsonify({
                'success': False,
                'message': f'以下集群仅剩 {blocked_upstreams[0]} 最后一台在线，不允许全部下线: {", ".join(blocked_upstreams)}'
            })

    if len(upstream_names) > 1:
        # 批量操作：同时修改多个 upstream
        success, message = modifier.set_server_status_batch(upstream_names, server_ip, server_port, online)
    else:
        # 单个操作
        success, message = modifier.set_server_status(upstream_names[0], server_ip, server_port, online)
    
    if not success:
        return jsonify({'success': False, 'message': message})
    
    # 幂等操作：若实际未修改配置，直接返回成功，跳过 SSH 写入和重载
    is_idempotent = '无需更改' in message or '已为' in message
    if is_idempotent:
        log = create_operation_log(
            action=action,
            upstream=','.join(upstream_names),
            server_ip=server_ip,
            port=server_port,
            success=True,
            message=f'幂等操作: {message}'
        )
        operation_logs.insert(0, log)
        _write_log_to_file(log, OPERATION_LOG_FILE)
        operation_logs[:] = operation_logs[:100]
        return jsonify({'success': True, 'message': message})

    new_config = modifier.get_content()
    
    # 写入配置并重载
    ssh = get_server_ssh(server)
    success, message = ssh.connect()
    
    if not success:
        return jsonify({'success': False, 'message': f'SSH 连接失败: {message}'})
    
    config_path = server.get('nginx_conf', '/etc/nginx/nginx.conf')
    success, message = ssh.write_file(config_path, new_config)
    
    if not success:
        ssh.close()
        return jsonify({'success': False, 'message': f'写入配置失败: {message}'})
    
    # 测试配置
    success, message = ssh.test_nginx_config()
    if not success:
        ssh.close()
        return jsonify({'success': False, 'message': f'Nginx 配置测试失败: {message}'})
    
    # 重载配置
    success, message = ssh.reload_nginx()
    ssh.close()
    
    if success:
        # 更新缓存
        current_config[server_id] = new_config
        
        # 记录日志
        log = create_operation_log(
            action=action,
            upstream=','.join(upstream_names),
            server_ip=server_ip,
            port=server_port,
            success=True,
            message=f'操作成功: {message}'
        )
        operation_logs.insert(0, log)
        _write_log_to_file(log, OPERATION_LOG_FILE)
        
        # 只保留最近 100 条日志
        operation_logs[:] = operation_logs[:100]
        
        return jsonify({'success': True, 'message': f'{server_ip}:{server_port} 已{"上线" if online else "下线"}'})
    else:
        return jsonify({'success': False, 'message': f'重载 Nginx 失败: {message}'})


@app.route('/api/logs', methods=['GET'])
def get_logs():
    """获取操作日志"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify({
        'success': True,
        'logs': operation_logs[:limit]
    })


# ==================== LVS 模块（完全独立）====================

# LVS 服务器 CRUD

@app.route('/api/lvs/servers', methods=['GET'])
def get_lvs_servers():
    """获取 LVS 服务器列表"""
    servers = load_lvs_servers()
    return jsonify({'success': True, 'data': servers})


@app.route('/api/lvs/servers', methods=['POST'])
def add_lvs_server():
    """添加 LVS 服务器"""
    data = request.json
    servers = load_lvs_servers()

    new_id = max([s.get('id', 0) for s in servers], default=0) + 1
    data['id'] = new_id

    servers.append(data)
    save_lvs_servers(servers)

    return jsonify({'success': True, 'message': 'LVS 服务器添加成功', 'id': new_id})


@app.route('/api/lvs/servers/<int:server_id>', methods=['PUT'])
def update_lvs_server(server_id):
    """更新 LVS 服务器"""
    data = request.json
    servers = load_lvs_servers()

    for i, s in enumerate(servers):
        if s.get('id') == server_id:
            data['id'] = server_id
            servers[i] = data
            save_lvs_servers(servers)
            return jsonify({'success': True, 'message': 'LVS 服务器更新成功'})

    return jsonify({'success': False, 'message': 'LVS 服务器不存在'})


@app.route('/api/lvs/servers/<int:server_id>', methods=['DELETE'])
def delete_lvs_server(server_id):
    """删除 LVS 服务器"""
    servers = load_lvs_servers()
    servers = [s for s in servers if s.get('id') != server_id]
    save_lvs_servers(servers)
    return jsonify({'success': True, 'message': 'LVS 服务器删除成功'})


@app.route('/api/lvs/servers/<int:server_id>/test', methods=['POST'])
def test_lvs_connection(server_id):
    """测试 LVS 服务器连接"""
    servers = load_lvs_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'LVS 服务器不存在'})

    success, message = test_connection(
        host=server['host'],
        port=server.get('port', 22),
        username=server['username'],
        password=server.get('password'),
        key_path=server.get('key_path')
    )

    return jsonify({'success': success, 'message': message})


# LVS 操作路由

@app.route('/api/lvs/<int:server_id>/status', methods=['GET'])
def get_lvs_status(server_id):
    """获取 LVS 完整状态（ipvsadm + keepalived 合并）"""
    servers = load_lvs_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'LVS 服务器不存在'})

    ssh = get_lvs_server_ssh(server)
    success, message = ssh.connect()

    if not success:
        return jsonify({'success': False, 'message': f'SSH 连接失败: {message}'})

    lvs_service = LVSService(
        ssh=ssh,
        ip_prefix=server.get('ip_prefix', '192.168.13.'),
        conf_dir=server.get('conf_dir', '/etc/keepalived')
    )

    success, result = lvs_service.get_lvs_status()
    ssh.close()

    if not success:
        return jsonify({'success': False, 'message': str(result)})

    return jsonify({
        'success': True,
        **result,
        'server_name': server.get('name', server.get('host'))
    })


@app.route('/api/lvs/<int:server_id>/op', methods=['POST'])
def lvs_rs_op(server_id):
    """RS 上线/下线操作，body: { vs_ip: '207', rs_ip: '215', state: 'on'|'off' }"""
    data = request.json
    vs_ip = data.get('vs_ip', '')
    rs_ip = data.get('rs_ip', '')
    state = data.get('state', '')

    if not (vs_ip and rs_ip and state in ('on', 'off')):
        return jsonify({'success': False, 'message': '参数不完整，需要 vs_ip, rs_ip, state(on/off)'})

    servers = load_lvs_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'LVS 服务器不存在'})

    ssh = get_lvs_server_ssh(server)
    success, message = ssh.connect()

    if not success:
        return jsonify({'success': False, 'message': f'SSH 连接失败: {message}'})

    lvs_service = LVSService(
        ssh=ssh,
        ip_prefix=server.get('ip_prefix', '192.168.13.'),
        conf_dir=server.get('conf_dir', '/etc/keepalived')
    )

    success, msg = lvs_service.set_rs_status(vs_ip, rs_ip, state)
    ssh.close()

    full_vs_ip = f"{lvs_service.ip_prefix}{vs_ip}"
    full_rs_ip = f"{lvs_service.ip_prefix}{rs_ip}"
    log = create_lvs_operation_log(
        action=f'op_{state}',
        vs_ip=full_vs_ip,
        rs_ip=full_rs_ip,
        success=success,
        message=msg
    )
    lvs_operation_logs.insert(0, log)
    _write_log_to_file(log, LVS_OPERATION_LOG_FILE)
    lvs_operation_logs[:] = lvs_operation_logs[:100]

    if success:
        return jsonify({'success': True, 'message': msg})
    else:
        return jsonify({'success': False, 'message': msg})


@app.route('/api/lvs/<int:server_id>/swap', methods=['POST'])
def lvs_rs_swap(server_id):
    """RS 互换操作，body: { vs_ip: '207', rs_ip1: '215', rs_ip2: '209' }"""
    data = request.json
    vs_ip = data.get('vs_ip', '')
    rs_ip1 = data.get('rs_ip1', '')
    rs_ip2 = data.get('rs_ip2', '')

    if not (vs_ip and rs_ip1 and rs_ip2):
        return jsonify({'success': False, 'message': '参数不完整，需要 vs_ip, rs_ip1, rs_ip2'})

    servers = load_lvs_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'LVS 服务器不存在'})

    ssh = get_lvs_server_ssh(server)
    success, message = ssh.connect()

    if not success:
        return jsonify({'success': False, 'message': f'SSH 连接失败: {message}'})

    lvs_service = LVSService(
        ssh=ssh,
        ip_prefix=server.get('ip_prefix', '192.168.13.'),
        conf_dir=server.get('conf_dir', '/etc/keepalived')
    )

    success, msg = lvs_service.swap_rs(vs_ip, rs_ip1, rs_ip2)
    ssh.close()

    full_vs_ip = f"{lvs_service.ip_prefix}{vs_ip}"
    full_rs1 = f"{lvs_service.ip_prefix}{rs_ip1}"
    full_rs2 = f"{lvs_service.ip_prefix}{rs_ip2}"
    log = create_lvs_operation_log(
        action='swap',
        vs_ip=full_vs_ip,
        rs_ip=f'{full_rs1}<->{full_rs2}',
        success=success,
        message=msg
    )
    lvs_operation_logs.insert(0, log)
    _write_log_to_file(log, LVS_OPERATION_LOG_FILE)
    lvs_operation_logs[:] = lvs_operation_logs[:100]

    if success:
        return jsonify({'success': True, 'message': msg})
    else:
        return jsonify({'success': False, 'message': msg})


@app.route('/api/lvs/logs', methods=['GET'])
def get_lvs_logs():
    """获取 LVS 操作日志"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify({
        'success': True,
        'logs': lvs_operation_logs[:limit]
    })


# ==================== ArgoCD 模块（完全独立）====================

# ArgoCD 服务器 CRUD

@app.route('/api/argocd/servers', methods=['GET'])
def get_argocd_servers():
    """获取 ArgoCD 服务器列表"""
    servers = load_argocd_servers()
    return jsonify({'success': True, 'data': servers})


@app.route('/api/argocd/servers', methods=['POST'])
def add_argocd_server():
    """添加 ArgoCD 服务器"""
    data = request.json
    servers = load_argocd_servers()

    new_id = max([s.get('id', 0) for s in servers], default=0) + 1
    data['id'] = new_id

    servers.append(data)
    save_argocd_servers(servers)

    return jsonify({'success': True, 'message': 'ArgoCD 服务器添加成功', 'id': new_id})


@app.route('/api/argocd/servers/<int:server_id>', methods=['PUT'])
def update_argocd_server(server_id):
    """更新 ArgoCD 服务器"""
    data = request.json
    servers = load_argocd_servers()

    for i, s in enumerate(servers):
        if s.get('id') == server_id:
            data['id'] = server_id
            servers[i] = data
            save_argocd_servers(servers)
            return jsonify({'success': True, 'message': 'ArgoCD 服务器更新成功'})

    return jsonify({'success': False, 'message': 'ArgoCD 服务器不存在'})


@app.route('/api/argocd/servers/<int:server_id>', methods=['DELETE'])
def delete_argocd_server(server_id):
    """删除 ArgoCD 服务器"""
    servers = load_argocd_servers()
    servers = [s for s in servers if s.get('id') != server_id]
    save_argocd_servers(servers)
    return jsonify({'success': True, 'message': 'ArgoCD 服务器删除成功'})


@app.route('/api/argocd/servers/<int:server_id>/test', methods=['POST'])
def test_argocd_connection(server_id):
    """测试 ArgoCD 服务器连接"""
    servers = load_argocd_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'ArgoCD 服务器不存在'})

    success, message = test_connection(
        host=server['host'],
        port=server.get('port', 22),
        username=server['username'],
        password=server.get('password'),
        key_path=server.get('key_path')
    )

    return jsonify({'success': success, 'message': message})


# ArgoCD Rollout 操作路由

@app.route('/api/argocd/<int:server_id>/rollouts', methods=['GET'])
def list_rollouts(server_id):
    """获取 Rollout 列表"""
    servers = load_argocd_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'ArgoCD 服务器不存在'})

    try:
        argocd_service = get_argocd_service(server)
        success, result = argocd_service.get_rollout_list()
        argocd_service.ssh.close()

        if not success:
            return jsonify({'success': False, 'message': str(result)})

        return jsonify({
            'success': True,
            **result,
            'server_name': server.get('name', server.get('host'))
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/argocd/<int:server_id>/action', methods=['POST'])
def argocd_action(server_id):
    """
    执行 ArgoCD 操作
    
    Body: { action: 'full_sync'|'full_rollback'|'full_online'|'single_sync'|'single_rollback'|'single_online',
           namespace: '', name: '' }   # single_* 时需要
    """
    data = request.json
    action = data.get('action', '')
    valid_actions = ['full_sync', 'full_rollback', 'full_online',
                     'single_sync', 'single_rollback', 'single_online']

    if action not in valid_actions:
        return jsonify({'success': False, 'message': f'无效的操作类型: {action}'})

    servers = load_argocd_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'ArgoCD 服务器不存在'})

    try:
        argocd_service = get_argocd_service(server)

        if action == 'full_sync':
            success, msg = argocd_service.full_sync()
            target = 'ALL'
        elif action == 'full_rollback':
            success, msg = argocd_service.full_rollback()
            target = 'ALL'
        elif action == 'full_online':
            success, msg = argocd_service.full_online()
            target = 'ALL'
        else:
            # single_* 操作
            ns = data.get('namespace', '')
            nm = data.get('name', '')
            if not (ns and nm):
                argocd_service.ssh.close()
                return jsonify({'success': False, 'message': 'single 操作需要 namespace 和 name 参数'})

            target = f'{ns}/{nm}'
            if action == 'single_sync':
                success, msg = argocd_service.promote_full(ns, nm)
            elif action == 'single_rollback':
                success, msg = argocd_service.undo(ns, nm)
            elif action == 'single_online':
                success, msg = argocd_service.promote(ns, nm)

        argocd_service.ssh.close()

        # 记录日志（同时写入 ArgoCD 专属日志和统一操作日志）
        log = create_argocd_operation_log(
            action=action,
            target=target,
            success=success,
            message=msg
        )
        argocd_operation_logs.insert(0, log)
        _write_log_to_file(log, ARGOCD_OPERATION_LOG_FILE)
        argocd_operation_logs[:] = argocd_operation_logs[:100]

        # 写入统一操作日志
        action_map = {
            'full_sync': '全量同步', 'full_rollback': '全量回滚', 'full_online': '全量上线',
            'single_sync': '同步', 'single_rollback': '回滚', 'single_online': '上线'
        }
        unified_log = {
            'timestamp': log['timestamp'],
            'module': 'k8s',
            'action': action_map.get(action, action),
            'server': target,
            'upstream': '',
            'success': success,
            'message': msg
        }
        operation_logs.insert(0, unified_log)
        _write_log_to_file(unified_log, OPERATION_LOG_FILE)
        operation_logs[:] = operation_logs[:100]

        if success:
            return jsonify({'success': True, 'message': msg})
        else:
            return jsonify({'success': False, 'message': msg})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/argocd/logs', methods=['GET'])
def get_argocd_logs():
    """获取 ArgoCD 操作日志"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify({
        'success': True,
        'logs': argocd_operation_logs[:limit]
    })






# ==================== K8s 扩缩容模块 ====================

@app.route('/api/scale/<int:server_id>/status', methods=['GET'])
def get_scale_status(server_id):
    """获取扩缩容资源列表当前状态"""
    from flask import Response as FlaskResponse
    servers = load_argocd_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'K8s 节点不存在'})

    try:
        ssh = SSHService(
            host=server['host'],
            port=server.get('port', 22),
            username=server['username'],
            password=server.get('password'),
            key_path=server.get('key_path')
        )
        ok, msg = ssh.connect()
        if not ok:
            return jsonify({'success': False, 'message': f'SSH 连接失败: {msg}'})

        scale_service = ScaleService(ssh=ssh)
        success, result = scale_service.list_resources()
        ssh.close()

        if not success:
            return jsonify({'success': False, 'message': str(result)})

        return jsonify({
            'success': True,
            **result,
            'server_name': server.get('name', server.get('host'))
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/scale/<int:server_id>/execute', methods=['POST'])
def scale_execute(server_id):
    """
    执行扩缩容操作（SSE 流式返回实时日志）

    Body: { action: 'scaledown'|'scaleup', password: 'xxx', resources?: ['namespace|name', ...] }
          resources 可选，为空或不存在时操作全部资源
    """
    from flask import Response as FlaskResponse
    data = request.json or {}
    action = data.get('action', '')
    password = data.get('password', '')
    selected_resources = data.get('resources')  # 可选，None 或 list

    if action not in ('scaledown', 'scaleup'):
        return jsonify({'success': False, 'message': '无效操作: ' + action})

    # 验证密码
    if not ScaleService.verify_password(password):
        return jsonify({'success': False, 'message': '密码错误'})

    servers = load_argocd_servers()
    server = next((s for s in servers if s.get('id') == server_id), None)

    if not server:
        return jsonify({'success': False, 'message': 'K8s 节点不存在'})

    def generate():
        try:
            ssh = SSHService(
                host=server['host'],
                port=server.get('port', 22),
                username=server['username'],
                password=server.get('password'),
                key_path=server.get('key_path')
            )
            ok, msg = ssh.connect()
            if not ok:
                yield "data: " + json.dumps({'type': 'error', 'message': 'SSH连接失败: ' + msg, 'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, ensure_ascii=False) + "\n\n"
                return

            scale_service = ScaleService(ssh=ssh)

            # 选择操作（传入可选的选中资源列表）
            sel_keys = selected_resources if selected_resources else None
            if action == 'scaledown':
                log_gen = scale_service.scaledown(selected_keys=sel_keys)
            else:
                log_gen = scale_service.scaleup(selected_keys=sel_keys)

            # 流式 yield 日志
            all_success = True
            final_msg = ''
            for log_entry in log_gen:
                yield f"data: {json.dumps(log_entry, ensure_ascii=False)}\n\n"
                if log_entry.get('type') == 'error':
                    all_success = False
                if log_entry.get('type') in ('success', 'error'):
                    final_msg = log_entry.get('message', '')

            ssh.close()

            # 记录操作日志
            log = create_scale_operation_log(
                action=action,
                success=all_success,
                message=final_msg
            )
            scale_operation_logs.insert(0, log)
            _write_log_to_file(log, SCALE_OPERATION_LOG_FILE)
            scale_operation_logs[:] = scale_operation_logs[:100]

            # 发送完成信号
            yield f"data: {json.dumps({'type': 'done', 'success': all_success, 'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, ensure_ascii=False)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'执行异常: {str(e)}', 'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, ensure_ascii=False)}\n\n"

    return FlaskResponse(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'  # 禁用 nginx 缓冲
        }
    )


@app.route('/api/scale/logs', methods=['GET'])
def get_scale_logs():
    """获取扩缩容操作日志"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify({
        'success': True,
        'logs': scale_operation_logs[:limit]
    })


@app.route('/api/log/error', methods=['POST'])
def log_error():
    """前端错误日志上报 - 写入本地文件"""
    try:
        data = request.get_json(silent=True) or {}
        message = data.get('message', '未知错误')
        module = data.get('module', 'unknown')
        detail = data.get('detail', '')

        # 确保 logs 目录存在
        os.makedirs(ERROR_LOG_DIR, exist_ok=True)

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_line = f"[{timestamp}] [{module.upper()}] {message}"
        if detail:
            log_line += f" | {detail}"
        log_line += "\n"

        with open(ERROR_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_line)

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


if __name__ == '__main__':
    print(f"""
╔════════════════════════════════════════════╗
║     Nginx Upstream Manager 启动中...       ║
╠════════════════════════════════════════════╣
║  访问地址: http://{HOST}:{PORT}              ║
║  调试模式: {'开启' if DEBUG else '关闭'}                       ║
╚════════════════════════════════════════════╝
    """)
    app.run(host=HOST, port=PORT, debug=DEBUG)
