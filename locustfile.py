import os
import random
import time
import uuid
from dataclasses import dataclass

from locust import HttpUser, between, events, task


SUBMIT_PATH = "/openapi/restful/capability/parse_doc/submit"
GET_INFO_PATH = "/openapi/restful/capability/parse_doc/get_task_info"

ACCOUNT_ID = os.getenv("ACCOUNT_ID", "openviking")
DOC_TYPE = os.getenv("DOC_TYPE", "glb")
SUB_PATH = os.getenv("SUB_PATH", "demo/")
VERSION = os.getenv("VERSION", "default")

# 你需要在运行前设置这三个环境变量。
# 例：export MODEL_URL_2MB='http://.../2mb.glb'
MODEL_URLS = {
    "2mb": os.getenv("MODEL_URL_2MB", ""),
    "20mb": os.getenv("MODEL_URL_20MB", ""),
    "250mb": os.getenv("MODEL_URL_250MB", ""),
}

# 默认少量 250MB，避免第一次就把队列打爆。
# 可通过 FILE_MIX 改，例如：FILE_MIX='2mb:80,20mb:20,250mb:0'
FILE_MIX = os.getenv("FILE_MIX", "2mb:70,20mb:25,250mb:5")

# 每个虚拟用户最多保留多少未完成任务用于轮询，防止本地 locust 内存无限增长。
MAX_TRACKED_TASKS_PER_USER = int(os.getenv("MAX_TRACKED_TASKS_PER_USER", "200"))

# 轮询权重：get_info 比 submit 高频，因为真实客户端通常会反复查状态。
SUBMIT_WEIGHT = int(os.getenv("SUBMIT_WEIGHT", "1"))
GET_INFO_WEIGHT = int(os.getenv("GET_INFO_WEIGHT", "5"))

DONE_STATUSES = {
    "已完成", "完成", "成功", "处理完成",
    "completed", "complete", "success", "succeeded", "done",
}
FAILED_STATUSES = {
    "失败", "处理失败", "failed", "fail", "error",
}


@dataclass
class SubmittedTask:
    task_id: str
    file_id: str
    url: str
    size_label: str
    submitted_at: float


def parse_file_mix(mix: str):
    items = []
    for part in mix.split(","):
        if not part.strip():
            continue
        label, weight = part.split(":", 1)
        label = label.strip().lower()
        weight = int(weight.strip())
        url = MODEL_URLS.get(label, "")
        if weight > 0 and url:
            items.append((label, url, weight))
    return items


WEIGHTED_FILES = parse_file_mix(FILE_MIX)


@events.init.add_listener
def validate_env(environment, **kwargs):
    if not WEIGHTED_FILES:
        raise RuntimeError(
            "没有可用的测试文件 URL。请至少设置一个环境变量："
            "MODEL_URL_2MB / MODEL_URL_20MB / MODEL_URL_250MB。"
        )


def choose_file():
    labels = [item[0] for item in WEIGHTED_FILES]
    urls = [item[1] for item in WEIGHTED_FILES]
    weights = [item[2] for item in WEIGHTED_FILES]
    idx = random.choices(range(len(labels)), weights=weights, k=1)[0]
    return labels[idx], urls[idx]


def find_value(obj, candidate_keys):
    """从未知返回结构里递归找字段，比如 task_id/status。"""
    if isinstance(obj, dict):
        for key in candidate_keys:
            if key in obj and obj[key] is not None:
                return obj[key]
        for value in obj.values():
            found = find_value(value, candidate_keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_value(item, candidate_keys)
            if found is not None:
                return found
    return None


def extract_task_id(resp_json):
    return find_value(resp_json, {"task_id", "taskId", "taskID", "id"})


def extract_status(resp_json):
    value = find_value(resp_json, {"status", "task_status", "taskStatus", "state"})
    return str(value).strip() if value is not None else ""


class ParseDocUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.tasks_by_id = {}

    @task(SUBMIT_WEIGHT)
    def submit(self):
        size_label, model_url = choose_file()
        short_id = uuid.uuid4().hex[:12]
        file_id = f"locust_{size_label}_{short_id}"
        logid = f"locust_submit_{size_label}_{short_id}"

        payload = {
            "account_id": ACCOUNT_ID,
            "url": model_url,
            "doc_type": DOC_TYPE,
            "file_id": file_id,
            "sub_path": SUB_PATH,
            "version": VERSION,
            "logid": logid,
        }

        with self.client.post(
            SUBMIT_PATH,
            json=payload,
            name=f"submit_{size_label}",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"submit HTTP {response.status_code}: {response.text[:300]}")
                return

            try:
                data = response.json()
            except Exception:
                response.failure(f"submit 返回不是 JSON: {response.text[:300]}")
                return

            task_id = extract_task_id(data)
            if not task_id:
                response.failure(f"submit 返回中找不到 task_id: {data}")
                return

            if len(self.tasks_by_id) < MAX_TRACKED_TASKS_PER_USER:
                self.tasks_by_id[str(task_id)] = SubmittedTask(
                    task_id=str(task_id),
                    file_id=file_id,
                    url=model_url,
                    size_label=size_label,
                    submitted_at=time.time(),
                )

            response.success()

    @task(GET_INFO_WEIGHT)
    def get_info(self):
        if not self.tasks_by_id:
            return

        task_id = random.choice(list(self.tasks_by_id.keys()))
        submitted = self.tasks_by_id[task_id]
        logid = f"locust_get_{submitted.size_label}_{uuid.uuid4().hex[:12]}"

        payload = {
            "account_id": ACCOUNT_ID,
            "task_id": submitted.task_id,
            "file_id": submitted.file_id,
            "sub_path": SUB_PATH,
            "version": VERSION,
            "doc_type": DOC_TYPE,
            "url": submitted.url,
            "logid": logid,
        }

        with self.client.post(
            GET_INFO_PATH,
            json=payload,
            name=f"get_info_{submitted.size_label}",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"get_info HTTP {response.status_code}: {response.text[:300]}")
                return

            try:
                data = response.json()
            except Exception:
                response.failure(f"get_info 返回不是 JSON: {response.text[:300]}")
                return

            status = extract_status(data)
            normalized = status.lower()

            if status in DONE_STATUSES or normalized in DONE_STATUSES:
                total_seconds = time.time() - submitted.submitted_at
                events.request.fire(
                    request_type="TASK",
                    name=f"task_done_{submitted.size_label}",
                    response_time=total_seconds * 1000,
                    response_length=0,
                    exception=None,
                    context={},
                )
                self.tasks_by_id.pop(task_id, None)
            elif status in FAILED_STATUSES or normalized in FAILED_STATUSES:
                total_seconds = time.time() - submitted.submitted_at
                events.request.fire(
                    request_type="TASK",
                    name=f"task_failed_{submitted.size_label}",
                    response_time=total_seconds * 1000,
                    response_length=0,
                    exception=Exception(f"task failed, status={status}"),
                    context={},
                )
                self.tasks_by_id.pop(task_id, None)

            response.success()
