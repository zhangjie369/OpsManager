"""
ArgoCD Rollouts 管理服务
封装 ArgoCD 应用列表查询、全量/单个/批量 同步、回滚、上线操作
基于 SSH 远程执行 kubectl argo rollouts 命令
"""
from typing import Tuple, Union, List, Dict
from datetime import datetime


class ArgoCDService:
    """ArgoCD Rollouts 管理服务类"""

    def __init__(self, ssh):
        """
        初始化 ArgoCD 服务

        Args:
            ssh: 已连接的 SSHService 实例（连接到 K8s 集群节点）
        """
        self.ssh = ssh

    def get_rollout_list(self) -> Tuple[bool, Union[str, Dict]]:
        """
        获取所有 Paused 状态的 Rollout 列表（对应 bash list 命令）

        Returns:
            成功时返回 (True, {rollouts: [...], summary: {...}})
            失败时返回 (False, 错误信息)
        """
        # 执行 kubectl argo rollouts list rollout -A
        success, output = self.ssh.execute('kubectl argo rollouts list rollout -A')
        if not success:
            return False, f'执行 kubectl argo rollouts list rollout -A 失败: {output}'

        # 解析输出
        all_rollouts = self._parse_rollout_list(output)

        # 只取 Paused 状态的
        paused_rollouts = [r for r in all_rollouts if r.get('status') == 'Paused']

        # 统计摘要
        syncable_count = sum(1 for r in paused_rollouts if r.get('can_sync'))
        onlineable_count = sum(1 for r in paused_rollouts if r.get('can_online'))

        return True, {
            'rollouts': paused_rollouts,
            'all_rollouts': all_rollouts,
            'summary': {
                'total_paused': len(paused_rollouts),
                'syncable': syncable_count,      # 3/5 等非 1/5 的可同步
                'onlineable': onlineable_count   # 1/5 可上线的
            }
        }

    def _parse_rollout_list(self, output: str) -> List[Dict]:
        """
        解析 kubectl argo rollouts list rollout -A 的输出

        实际输出格式（kubectl argo rollouts >= v1.1）：
        NAMESPACE     NAME          STRATEGY   STATUS      STEP   SET-WEIGHT  READY  DESIRED  UP-TO-DATE  AVAILABLE
        pro-frontend  my-rollout    Canary     Paused      3/5    100         4/4    2        2           4
        """
        rollouts = []
        lines = output.strip().split('\n')

        for line in lines:
            line = line.strip()
            if not line or line.startswith('NAMESPACE'):
                continue

            parts = line.split()
            if len(parts) >= 10:
                namespace = parts[0]
                name = parts[1]
                # strategy = parts[2]  # Canary / BlueGreen
                status = parts[3]       # Paused / Healthy / Running 等
                stepset_ready = parts[4] if len(parts) > 4 else ''   # 如 3/5
                # set_weight = parts[5] if len(parts) > 5 else ''
                # ready = parts[6] if len(parts) > 6 else ''
                # desired = parts[7] if len(parts) > 7 else ''
                # up_to_date = parts[8] if len(parts) > 8 else ''
                # available = parts[9] if len(parts) > 9 else ''

                # 判断是否可同步(3/5等非1/5)或可上线(1/5)
                can_sync = False
                can_online = False
                if status == 'Paused':
                    # 解析 stepset_ready (如 "3/5", "1/5", "5/5")
                    if '/' in stepset_ready:
                        try:
                            current, total = map(int, stepset_ready.split('/'))
                            can_sync = (current > 1 and current != total)  # 中间状态可同步
                            can_online = (current == 1)                 # 1/5 可上线
                        except ValueError:
                            pass

                rollouts.append({
                    'namespace': namespace,
                    'name': name,
                    'status': status,
                    'stepset_ready': stepset_ready,
                    'can_sync': can_sync,
                    'can_online': can_online
                })

        return rollouts

    def promote_full(self, namespace: str, name: str) -> Tuple[bool, str]:
        """单个应用全量同步 promote --full"""
        cmd = f'kubectl argo rollouts promote --full {name} -n {namespace}'
        return self._exec_action(cmd, f'全量同步', name, namespace)

    def promote(self, namespace: str, name: str) -> Tuple[bool, str]:
        """单个应用上线 promote"""
        cmd = f'kubectl argo rollouts promote {name} -n {namespace}'
        return self._exec_action(cmd, '上线', name, namespace)

    def undo(self, namespace: str, name: str) -> Tuple[bool, str]:
        """单个应用回滚"""
        cmd = f'kubectl argo rollouts undo {name} -n {namespace}'
        return self._exec_action(cmd, '回滚', name, namespace)

    def full_sync(self) -> Tuple[bool, str]:
        """全量同步：对所有 3/5 等 Paused 状态的应用执行 promote --full"""
        # 获取当前列表
        success, result = self.get_rollout_list()
        if not success:
            return False, result

        syncable = [r for r in result['rollouts'] if r.get('can_sync')]
        if not syncable:
            return False, '没有需要同步的应用（无 Paused 且非 1/5 的 Rollout）'

        results = []
        for r in syncable:
            ok, msg = self.promote_full(r['namespace'], r['name'])
            results.append(f"{r['namespace']}/{r['name']}: {'OK' if ok else msg}")

        return True, f"已对 {len(syncable)} 个应用执行全量同步\n" + "\n".join(results)

    def full_rollback(self) -> Tuple[bool, str]:
        """全量回滚"""
        success, result = self.get_rollout_list()
        if not success:
            return False, result

        rollback_targets = [r for r in result['rollouts'] if r.get('can_sync')]
        if not rollback_targets:
            return False, '没有需要回滚的应用'

        results = []
        for r in rollback_targets:
            ok, msg = self.undo(r['namespace'], r['name'])
            results.append(f"{r['namespace']}/{r['name']}: {'OK' if ok else msg}")

        return True, f"已对 {len(rollback_targets)} 个应用执行回滚\n" + "\n".join(results)

    def full_online(self) -> Tuple[bool, str]:
        """全量上线：对所有 1/5 状态的应用执行 promote"""
        success, result = self.get_rollout_list()
        if not success:
            return False, result

        onlineable = [r for r in result['rollouts'] if r.get('can_online')]
        if not onlineable:
            return False, '没有需要上线的应用（无 1/5 状态的 Rollout）'

        results = []
        for r in onlineable:
            ok, msg = self.promote(r['namespace'], r['name'])
            results.append(f"{r['namespace']}/{r['name']}: {'OK' if ok else msg}")

        return True, f"已对 {len(onlineable)} 个应用执行上线\n" + "\n".join(results)

    def batch_promote_full(self, items: List[Dict]) -> Tuple[bool, str]:
        """
        批量全量同步

        Args:
            items: [{'namespace': 'xxx', 'name': 'yyy'}, ...]
        """
        if not items:
            return False, '批量列表为空'

        results = []
        success_count = 0
        for item in items:
            ns = item.get('namespace', '')
            nm = item.get('name', '')
            if not ns or not nm:
                results.append(f"跳过无效项: {item}")
                continue
            ok, msg = self.promote_full(ns, nm)
            if ok:
                success_count += 1
                results.append(f"{ns}/{nm}: OK")
            else:
                results.append(f"{ns}/{nm}: FAIL - {msg}")

        return True, f"批量全量同步完成 ({success_count}/{len(items)})\n" + "\n".join(results)

    def batch_undo(self, items: List[Dict]) -> Tuple[bool, str]:
        """批量回滚"""
        if not items:
            return False, '批量列表为空'

        results = []
        success_count = 0
        for item in items:
            ns = item.get('namespace', '')
            nm = item.get('name', '')
            if not ns or not nm:
                continue
            ok, msg = self.undo(ns, nm)
            if ok:
                success_count += 1
                results.append(f"{ns}/{nm}: OK")
            else:
                results.append(f"{ns}/{nm}: FAIL - {msg}")

        return True, f"批量回滚完成 ({success_count}/{len(items)})\n" + "\n".join(results)

    def batch_promote(self, items: List[Dict]) -> Tuple[bool, str]:
        """批量上线"""
        if not items:
            return False, '批量列表为空'

        results = []
        success_count = 0
        for item in items:
            ns = item.get('namespace', '')
            nm = item.get('name', '')
            if not ns or not nm:
                continue
            ok, msg = self.promote(ns, nm)
            if ok:
                success_count += 1
                results.append(f"{ns}/{nm}: OK")
            else:
                results.append(f"{ns}/{nm}: FAIL - {msg}")

        return True, f"批量上线完成 ({success_count}/{len(items)})\n" + "\n".join(results)

    def _exec_action(self, cmd: str, action_name: str, name: str, namespace: str) -> Tuple[bool, str]:
        """执行单个 kubectl 操作"""
        success, output = self.ssh.execute(cmd)
        if success:
            return True, f'{action_name}成功: {namespace}/{name}'
        else:
            return False, f'{action_name}失败: {namespace}/{name} - {output}'


def create_argocd_operation_log(action: str, target: str, success: bool, message: str) -> Dict:
    """创建 ArgoCD 操作日志记录"""
    return {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,       # 'list'/'full_sync'/'full_rollback'/'full_online'/'single_sync'/'single_rollback'/'single_online'
        'module': 'argocd',
        'target': target,       # 'ALL' 或 'namespace/name'
        'success': success,
        'message': message
    }
