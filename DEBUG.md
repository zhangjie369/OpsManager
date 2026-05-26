# Debug 记录

## 2026-05-13 批量下线功能改造 + SVG 报错修复

### 1. SVG path 弧线指令报错（已修复 ✅）
**现象**：浏览器 Console 报错 `Error: <path> attribute d: Expected arc flag ('0' or '1'), "…57-1.657a2 2 0 022.828 0l2.829 2…"`
**原因**：SVG 弧线指令 `a rx ry x-rot large-arc-flag sweep-flag dx dy` 中，`sweep-flag`(第5个参数) 与 `dx`(第6个参数) 之间缺少空格。浏览器将 `012.828` 解析为非法弧线标志值。
**修复**：将所有 `012.828` 改为 `01 2.828`
**涉及位置**（共5处）：
- 行109: Nginx tab 图标 (stroke-width=2)
- 行496: K8s 侧边栏图标 (stroke-width=2)
- 行649: K8s 空状态图标 (stroke-width=1.5)
- 行1104: K8s 卡片图标 (stroke-width=2)
- 行1172: LVS 空状态图标 (stroke-width=1.5)

### 2. 批量下线选择弹窗改造（已完成 ✅）
**背景**：原弹窗按 upstream 集群分组展示服务器列表，用户反馈看不懂、信息冗余。

**需求变更历程**：
1. **第一版**：按集群分组 → 用户说"怎么跳出来两个集群"（同一IP在不同集群重复出现）
2. **第二版**：按 IP 分组，每个 IP 下展开其所属集群供勾选 → 用户说"不需要这么复杂"
3. **最终版（当前）**：纯 IP 列表，简洁勾选

**最终方案**：
- 勾选多台机器点批量下线时，如果某集群的在线机器全部被选中 → 触发冲突弹窗
- 弹窗只列出 IP 列表（如 `your-server-ip:80`、`your-server-ip2:80`），每行显示该 IP 属于几个集群
- 默认全选，用户取消勾选某台 → 该 IP 在**所有集群**中都保留不下线
- 确认后对选中 IP 在其所属的全部集群执行下线操作

**修改文件**：
- `templates/index.html`:
  - `confirmBatchAction('down')`: 数据构建从按集群分组改为按 IP 分组
  - `executeBatchDownSelect()`: 遍历选中 IP 的所有集群生成下线任务
  - `toggleDownSelect()`: 简化为切换单个 IP 的 selected 状态
  - HTML 模板：从嵌套分组改为扁平 IP 列表

### 3. 待排查问题（⚠️）
**现象**：批量下线确认后右下角 toast 出现报错信息但一闪而过看不清
**可能原因**：
- 新路径 `executeBatchDownSelect()` 跳过了正常路径中的安全过滤逻辑（最后一台在线保护）
- 后端 API 返回 `success: false` 时会触发 `showToast(data.message, 'error')`
**建议**：复现时打开 F12 Console 查看完整错误信息

---
