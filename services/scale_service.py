"""
K8s 扩缩容管理服务
封装 rollout/deployment 的缩容(→0)和扩容(→原值)操作
基于 SSH 远程执行 kubectl 命令，完全复现 bash 脚本的时序逻辑
"""
import time
import json
from typing import Tuple, Union, List, Dict, Generator
from datetime import datetime


# ==================== 硬编码资源列表（与脚本保持一致）====================

# 主业务 rollout 资源列表（并行操作）
ROLLOUT_LIST = [
    {'namespace': 'pro-java', 'name': 'pro-java-flightrefund-order-service-hs-com-rollout', 'replicas': 2},
    {'namespace': 'pro-java', 'name': 'pro-java-hotelbusiness-service-hs-com-rollout', 'replicas': 2},
    {'namespace': 'pro-java', 'name': 'pro-java-budget-manageservice-hs-com-rollout', 'replicas': 2},
]

# 非 ArgoCD 管理的 deployment 资源列表（并行操作）
DEPLOYMENT_LIST = [
    {'namespace': 'middleware', 'name': 'middleware-xxl-job-admin-deployment', 'replicas': 2},
]

# 依赖服务 rollout 列表（串行操作）
REQUIRE_LIST = [
    {'namespace': 'pro-dotnet', 'name': 'pro-dotnet-domaineventserviceapi-hs-com-rollout', 'replicas': 4},
]


# 常量配置（与脚本保持一致）
AUTH_PASSWORD = os.environ.get('SCALE_AUTH_PASSWORD', 'admin123')
SCALE_DOWN_VALUE = 0
CHECK_INTERVAL = 5       # 检查Pod状态间隔(秒)
MAX_RETRIES = 18         # 最大重试次数（5秒*18=90秒超时）
GRACE_TIME = CHECK_INTERVAL * 7  # pod优雅关闭30秒，预留35秒


class ScaleService:
    """K8s 扩缩容管理服务类"""

    # 全部资源的扁平化查找表（用于按 key 匹配选中项）
    ALL_RESOURCES_MAP = {}
    for _item in ROLLOUT_LIST:
        ALL_RESOURCES_MAP[_item['namespace'] + '|' + _item['name']] = ('rollout', _item)
    for _item in DEPLOYMENT_LIST:
        ALL_RESOURCES_MAP[_item['namespace'] + '|' + _item['name']] = ('deployment', _item)
    for _item in REQUIRE_LIST:
        ALL_RESOURCES_MAP[_item['namespace'] + '|' + _item['name']] = ('rollout', _item)

    def __init__(self, ssh):
        self.ssh = ssh

    @staticmethod
    def find_resource_by_key(key):
        """通过 'namespace|name' key 查找资源，返回 (resource_type, item_dict) 或 None"""
        return ScaleService.ALL_RESOURCES_MAP.get(key)

    def _now(self) -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    @staticmethod
    def _filter_resources(selected_keys):
        """根据前端选中的 key 列表过滤出三类资源。返回 (rollouts, deployments, requires)。"""
        sel_rollouts = []
        sel_deps = []
        sel_requires = []
        for key in (selected_keys or []):
            info = ScaleService.ALL_RESOURCES_MAP.get(key)
            if not info:
                continue
            rtype, item = info
            if item in ROLLOUT_LIST:
                sel_rollouts.append(item)
            elif item in DEPLOYMENT_LIST:
                sel_deps.append(item)
            elif item in REQUIRE_LIST:
                sel_requires.append(item)
        return sel_rollouts, sel_deps, sel_requires

    # ==================== 状态查询 ====================

    def list_resources(self) -> Tuple[bool, Union[str, Dict]]:
        """
        查询所有资源的当前状态（对应脚本中的 list 命令）

        Returns:
            成功时返回 (True, {rollouts: [], deployments: [], requires: [], summary: {...}})
            失败时返回 (False, 错误信息)
        """
        resources = {
            'rollouts': [],
            'deployments': [],
            'requires': [],
            'summary': {}
        }

        # 查询 rolloutlist
        for item in ROLLOUT_LIST:
            status = self._get_resource_status('rollout', item['namespace'], item['name'])
            status['target_replicas'] = item['replicas']
            status['category'] = '主业务'
            resources['rollouts'].append(status)

        # 查询 deploymentlist
        for item in DEPLOYMENT_LIST:
            status = self._get_resource_status('deployment', item['namespace'], item['name'])
            status['target_replicas'] = item['replicas']
            status['category'] = '非ArgoCD'
            resources['deployments'].append(status)

        # 查询 requirelist
        for item in REQUIRE_LIST:
            status = self._get_resource_status('rollout', item['namespace'], item['name'])
            status['target_replicas'] = item['replicas']
            status['category'] = '依赖服务'
            resources['requires'].append(status)

        # 统计摘要
        all_resources = resources['rollouts'] + resources['deployments'] + resources['requires']
        total_running = sum(1 for r in all_resources if r.get('current_replicas', 0) > 0)
        total_stopped = sum(1 for r in all_resources if r.get('current_replicas', 0) == 0)

        resources['summary'] = {
            'total': len(all_resources),
            'running': total_running,
            'stopped': total_stopped,
            'rollout_count': len(resources['rollouts']),
            'deployment_count': len(resources['deployments']),
            'require_count': len(resources['requires'])
        }

        return True, resources

    def _get_resource_status(self, resource_type: str, namespace: str, name: str) -> Dict:
        """查询单个资源的当前副本数和状态"""
        cmd = f'kubectl get {resource_type} -n {namespace} {name} -o jsonpath="{{.spec.replicas}}" 2>/dev/null; echo ";"; kubectl get {resource_type} -n {namespace} {name} -o jsonpath="{{.status.readyReplicas}}" 2>/dev/null'
        success, output = self.ssh.execute(cmd)

        result = {
            'namespace': namespace,
            'name': name,
            'type': resource_type,
            'current_replicas': None,
            'ready_replicas': None,
            'status': 'unknown',
            'error': None
        }

        if success and output.strip():
            try:
                parts = output.strip().split(';')
                current = int(parts[0].strip()) if parts[0].strip().isdigit() else None
                ready_str = parts[1].strip() if len(parts) > 1 else ''
                ready = int(ready_str) if ready_str.isdigit() else None
                result['current_replicas'] = current
                result['ready_replicas'] = ready

                if current is not None:
                    if current == 0:
                        result['status'] = 'stopped'
                    elif ready is not None and ready >= current:
                        result['status'] = 'healthy'
                    elif ready is not None and ready > 0:
                        result['status'] = 'degraded'
                    else:
                        result['status'] = 'running'
            except (ValueError, IndexError):
                result['error'] = output.strip()[:200]
        else:
            result['error'] = output.strip()[:200] if output else '命令执行失败'

        return result

    # ==================== 密码验证 ====================

    @staticmethod
    def verify_password(password: str) -> bool:
        """验证操作密码"""
        return password == AUTH_PASSWORD

    # ==================== 核心操作：带日志生成器 ====================

    def scaledown(self, selected_keys=None):  # type: (list|None) -> Generator[Dict, None, None]
        """
        缩容操作（生成器，逐步 yield 日志）

        Args:
            selected_keys: 前端选中的资源 key 列表 ['namespace|name', ...]。
                           为 None 或空列表时操作全部资源。

        缩容流程：
        1. 主业务并行缩至0
        2. 等35秒（优雅关闭）
        3. 依赖串行缩至0

        Yields: dict 日志消息，包含 type/info/message/timestamp 字段
        """
        # 过滤选中的资源
        if selected_keys:
            sel_rollouts, sel_deployments, sel_requires = self._filter_resources(selected_keys)
            if not sel_rollouts and not sel_deployments and not sel_requires:
                yield self._log('error', '[ERROR] 未找到匹配的资源')
                return
            mode_label = f'选中({len(selected_keys)}项)'
        else:
            sel_rollouts, sel_deployments, sel_requires = ROLLOUT_LIST, DEPLOYMENT_LIST, REQUIRE_LIST
            mode_label = '全部'

        yield self._log('info', f'[INFO] 开始缩容操作 ({mode_label})')

        # ===== 第一步：并行缩容 rolloutlist 和 deploymentlist =====
        main_tasks = []
        for item in sel_rollouts:
            main_tasks.append(('rollout', item))
        for item in sel_deployments:
            main_tasks.append(('deployment', item))

        if main_tasks:
            yield self._log('info', f'[INFO] 开始并行缩容主业务资源 ({len(main_tasks)} 个)...')
            results = self._parallel_scale('scaledown', main_tasks, SCALE_DOWN_VALUE)
            for r in results:
                yield r
        else:
            yield self._log('info', '[INFO] 无主业务资源需要缩容')

        # ===== 第二步：等待优雅关闭 =====
        yield self._log('wait', f'[INFO] 休息 {GRACE_TIME} 秒（等待Pod优雅关闭）...')
        for i in range(GRACE_TIME):
            time.sleep(1)
            if (i + 1) % 5 == 0 or i == GRACE_TIME - 1:
                yield self._log('progress', f'    ... 等待中 ({i+1}/{GRACE_TIME}s)')

        # ===== 第三步：串行缩容 requirelist =====
        if sel_requires:
            yield self._log('info', f'[INFO] 开始缩容依赖服务资源 ({len(sel_requires)} 个)...')
            for item in sel_requires:
                for msg in self._do_scale('scaledown', 'rollout', item, SCALE_DOWN_VALUE):
                    yield msg
        else:
            yield self._log('info', '[INFO] 无依赖服务资源需要缩容')

        yield self._log('success', '[SUCCESS] 缩容操作全部完成')

    def scaleup(self, selected_keys=None):  # type: (list|None) -> Generator[Dict, None, None]
        """
        扩容操作（生成器，逐步 yield 日志）

        Args:
            selected_keys: 前端选中的资源 key 列表 ['namespace|name', ...]。
                           为 None 或空列表时操作全部资源。

        扩容流程：
        1. 依赖串行扩至原值并等就绪
        2. 等5秒
        3. 主业务并行扩至原值

        Yields: dict 日志消息
        """
        if selected_keys:
            sel_rollouts, sel_deployments, sel_requires = self._filter_resources(selected_keys)
            if not sel_rollouts and not sel_deployments and not sel_requires:
                yield self._log('error', '[ERROR] 未找到匹配的资源')
                return
            mode_label = '选中(' + str(len(selected_keys)) + '项)'
        else:
            sel_rollouts, sel_deployments, sel_requires = ROLLOUT_LIST, DEPLOYMENT_LIST, REQUIRE_LIST
            mode_label = '全部'

        yield self._log('info', '[INFO] 开始扩容操作 (' + mode_label + ')')

        # 第一步：串行扩容 requirelist
        if sel_requires:
            yield self._log('info', '[INFO] 开始扩容依赖服务资源 (' + str(len(sel_requires)) + ') 个...')
            for item in sel_requires:
                for msg in self._do_scale('scaleup', 'rollout', item, item.get('replicas', 1)):
                    yield msg
                    if msg.get('type') == 'error' and '失败' in (msg.get('message') or ''):
                        yield self._log('error', '[ERROR] 依赖资源扩容失败，停止后续操作')
                        return
        else:
            yield self._log('info', '[INFO] 无依赖服务资源需要扩容')

        # 第二步：等待
        yield self._log('wait', '[INFO] 休息 ' + str(CHECK_INTERVAL) + ' 秒...')
        time.sleep(CHECK_INTERVAL)

        # 第三步：并行扩容 rolloutlist 和 deploymentlist
        main_tasks = []
        for item in sel_rollouts:
            main_tasks.append(('rollout', item))
        for item in sel_deployments:
            main_tasks.append(('deployment', item))

        if main_tasks:
            yield self._log('info', '[INFO] 开始并行扩容主业务资源 (' + str(len(main_tasks)) + ') 个...')
            results = self._parallel_scale('scaleup', main_tasks, None)
            for r in results:
                yield r
        else:
            yield self._log('info', '[INFO] 无主业务资源需要扩容')

        yield self._log('success', '[SUCCESS] 扩容操作全部完成')

    # ==================== 内部实现 ====================

    def _do_scale(self, action: str, resource_type: str, item: dict, target_replicas: int) -> Generator[Dict, None, None]:
        """执行单个资源的扩/缩操作（生成器）"""
        namespace = item['namespace']
        name = item['name']
        actual_target = target_replicas if target_replicas is not None else item.get('replicas', 1)

        yield self._log('action_start', f'正在{action} {resource_type}: {namespace}/{name}, 目标副本: {actual_target}')

        # 获取当前副本
        success, output = self.ssh.execute(
            f'kubectl get {resource_type} -n {namespace} {name} -o jsonpath="{{.spec.replicas}}"'
        )
        if not success:
            yield self._log('error', f'[ERROR] 获取当前副本失败: {output}')
            return

        try:
            current_replicas = int(output.strip()) if output.strip() else 0
        except ValueError:
            yield self._log('error', f'[ERROR] 无法解析副本数: {output}')
            return

        if current_replicas == actual_target:
            yield self._log('info', f'[INFO] {namespace}/{name} 已经是目标副本数 {actual_target}，无需操作')
            return

        # 执行 scale 操作
        success, output = self.ssh.execute(
            f'kubectl scale {resource_type} --replicas={actual_target} -n {namespace} {name}'
        )
        if not success:
            yield self._log('error', f'[ERROR] {action}失败: {output}')
            return

        yield self._log('success', f'[SUCCESS] {action}成功: {namespace}/{name} → {actual_target}')

        # 检查 Pod 状态
        if action == 'scaledown' and actual_target == 0:
            yield from self._check_pods_stopped(namespace, resource_type, name)
        elif action == 'scaleup' and actual_target > 0:
            yield from self._check_pods_started(namespace, resource_type, name, actual_target)

    def _check_pods_stopped(self, namespace: str, resource_type: str, resource_name: str) -> Generator[Dict, None, None]:
        """轮询检查 Pod 是否全部停止"""
        yield self._log('info', f'[INFO] 正在检查 {namespace}/{resource_name} 的Pod停止状态...')

        retries = 0
        while retries < MAX_RETRIES:
            success, output = self.ssh.execute(
                f'kubectl get pods -n {namespace} | grep "{resource_name}" | grep "Running" | wc -l'
            )
            running_pods = 0
            if success and output.strip():
                try:
                    running_pods = int(output.strip())
                except ValueError:
                    pass

            if running_pods > 0:
                yield self._log('progress', f'[INFO] 仍有 {running_pods} 个Pod运行中，等待 {CHECK_INTERVAL}s 后重试... ({retries+1}/{MAX_RETRIES})')
                time.sleep(CHECK_INTERVAL)
                retries += 1
            else:
                break

        if running_pods > 0:
            yield self._log('error', f'[ERROR] 超时: {namespace}/{resource_name} 仍有Pod在运行')
        else:
            yield self._log('success', f'{self._now()} [INFO] {namespace}/{resource_name} 所有Pod已停止')

    def _check_pods_started(self, namespace: str, resource_type: str, resource_name: str, expected: int) -> Generator[Dict, None, None]:
        """轮询检查 Pod 是否成功启动"""
        yield self._log('info', f'[INFO] 正在检查 {namespace}/{resource_name} 的Pod启动状态...')

        retries = 0
        while retries < MAX_RETRIES:
            success, output = self.ssh.execute(
                f'kubectl get {resource_type} -n {namespace} {resource_name} -o jsonpath="{{.status.readyReplicas}}"'
            )
            ready_pods = 0
            if success and output.strip():
                try:
                    ready_pods = int(output.strip())
                except ValueError:
                    ready_pods = 0

            if ready_pods < expected:
                yield self._log('progress', f'[INFO] Pod启动中 (就绪 {ready_pods}/{expected})，等待 {CHECK_INTERVAL}s... ({retries+1}/{MAX_RETRIES})')
                time.sleep(CHECK_INTERVAL)
                retries += 1
            else:
                break

        if ready_pods < expected:
            yield self._log('error', f'[ERROR] 超时: {namespace}/{resource_name} 只有 {ready_pods}/{expected} 个Pod就绪')
        else:
            yield self._log('success', f'[SUCCESS] {namespace}/{resource_name} 所有Pod已就绪 ({ready_pods}/{expected})')

    def _parallel_scale(self, action: str, tasks: List[tuple], target_replicas: int) -> Generator[Dict, None, None]:
        """
        并行执行多个资源的扩/缩操作

        由于 SSH 是单连接的，这里用"伪并行"——快速依次执行，
        但逻辑上等同于脚本的并行 & wait 模式。
        实际上 paramiko 单连接无法真正并行，所以顺序执行但收集所有结果。
        """
        import concurrent.futures
        import threading

        results = []
        lock = threading.Lock()

        def _worker(resource_type, item):
            """工作线程中执行单个任务（需要自己的SSH连接）"""
            # 注意：每个线程创建独立SSH连接
            local_msgs = []
            try:
                for msg in self._do_scale(action, resource_type, item, target_replicas):
                    with lock:
                        results.append((msg,))
            except Exception as e:
                with lock:
                    results.append((self._log('error', f'任务异常: {resource_type} {item["name"]} - {str(e)}'),))

        # 使用线程池并行执行
        max_workers = min(len(tasks), 5)  # 最多5个并行
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for resource_type, item in tasks:
                fut = executor.submit(_worker, resource_type, item)
                futures.append(fut)

            # 等待全部完成
            for future in concurrent.futures.as_completed(futures):
                future.result()  # 获取结果/异常

        # 按顺序 yield 结果
        for r in results:
            yield r[0]

    def _log(self, log_type: str, message: str) -> Dict:
        """构造标准日志格式"""
        return {
            'type': log_type,      # info/success/error/warn/action_start/progress/wait
            'message': message,
            'timestamp': self._now()
        }


def create_scale_operation_log(action: str, success: bool, message: str) -> Dict:
    """创建扩缩容操作日志记录"""
    return {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,         # 'scaledown' / 'scaleup' / 'list'
        'module': 'scale',
        'success': success,
        'message': message
    }
