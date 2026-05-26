# Nginx Upstream Manager

统一运维管理平台，支持 Nginx upstream、LVS Keepalived、ArgoCD Rollout 和 K8s 扩缩容的集中管理。

## 功能特性

- 🎯 **Nginx 管理** - 集群状态查看、服务器上线/下线/交换
- 🔀 **LVS 管理** - 虚拟服务器拓扑、RS 上线/下线/交换
- 🚀 **ArgoCD 管理** - Rollout 状态监控、同步/回滚/上线
- 📊 **K8s 扩缩容** - Deployment/Rollout 批量启停，支持依赖服务顺序操作
- 📝 **统一操作日志** - 记录所有模块的操作历史，支持筛选/搜索/导出
- 🔐 **SSH 加密连接** - 密码使用 Fernet 加密存储
- 📱 **响应式设计** - 桌面和移动端均可使用

## 技术栈

- **后端**: Python Flask
- **SSH 连接**: paramiko
- **前端**: HTML + Tailwind CSS + Alpine.js (CDN)
- **配置加密**: cryptography (Fernet)

## 快速开始

### 环境要求

- Python 3.8+
- 可访问目标服务器的 SSH 连接

### 1. 克隆项目

```bash
git clone https://github.com/your-username/nginx-manager.git
cd nginx-manager
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量（可选）

```bash
# 扩缩容操作密码（必填，默认 admin123）
set SCALE_AUTH_PASSWORD=your-password

# Flask 密钥（用于 session，生产环境必改）
set SECRET_KEY=your-random-secret-key
```

### 4. 启动服务

```bash
python app.py
```

首次运行会自动生成加密密钥 `.key` 文件。

### 5. 访问界面

打开浏览器: http://localhost:5000

### 6. 添加服务器

通过 Web 界面配置 SSH 连接信息（IP、端口、用户名、密码/密钥），配置文件自动保存到 `servers.json`、`lvs_servers.json`、`argocd_servers.json`。

## 项目结构

```
nginx-manager/
├── app.py                    # Flask 主应用
├── config.py                 # 配置管理（密钥、服务器加载/保存）
├── services/
│   ├── ssh_service.py        # SSH 连接服务
│   ├── nginx_service.py      # Nginx 配置解析/修改
│   ├── lvs_service.py        # LVS Keepalived 管理
│   ├── argocd_service.py     # ArgoCD Rollout 管理
│   └── scale_service.py      # K8s 扩缩容操作
├── templates/
│   └── index.html            # 单页 Web 界面
├── requirements.txt          # Python 依赖
└── README.md                 # 说明文档
```

## 使用说明

### 服务器配置页
- 添加/编辑/删除服务器（Nginx、LVS、ArgoCD 分别配置）
- 拖拽排序
- SSH 连接测试

### Nginx 管理
- 查看所有 upstream 集群及服务器状态
- 单台/批量上线/下线
- 集群间服务器交换（Swap）

### LVS 管理
- 查看 VS(虚拟服务器) 和 RS(真实服务器) 拓扑
- RS 上线/下线
- 批量操作

### ArgoCD 管理
- 查看 Rollout 状态（running/degraded/paused）
- 单应用同步/回滚/上线
- 全量同步/回滚/上线

### K8s 扩缩容
- 查看主业务和依赖服务的启停状态
- 单资源/批量启动/停止
- 依赖服务检查（启动主业务前检测依赖是否就绪）
- 实时操作日志 SSE 流式输出

## 安全注意

- SSH 密码使用 Fernet 加密存储到 `*_servers.json`
- 加密密钥 `.key` 已加入 `.gitignore`，不会提交到仓库
- `*_servers.json` 已加入 `.gitignore`，需在各环境中自行配置
- 生产环境请配置 HTTPS 和强密码
- 扩缩容密码通过环境变量 `SCALE_AUTH_PASSWORD` 设置

## License

MIT
