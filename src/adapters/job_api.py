from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.application.dispatch import JobDispatcher


class JobApi:
    """
    외부 시스템(Agent C UI / HTTP 서버 등)이 호출하는 dispatch 진입점의
    함수형 어댑터. 현재 시뮬레이터는 메시지 버스 기반이라 HTTP 서버를
    직접 띄우지 않는다. 이 클래스는 JobDispatcher를 감싸 dict-in / dict-out
    스키마를 고정해 두어 추후 FastAPI 등 HTTP 레이어를 그대로 위에 얹을
    수 있도록 한다.

    스키마 (Agent C 전달용 — 변경 시 양측 합의 필요):

      POST /job/dispatch
        request:  {"amr_id": str, "destination_node_id": str, "sim_time": float?}
        response: {
          "job_id": str | null,
          "status": "success" | "amr_not_found" | "amr_busy"
                     | "node_not_found" | "no_path",
          "estimated_arrival_s": float | null,
          "reason": str | null,
        }
    """

    def __init__(self, dispatcher: "JobDispatcher") -> None:
        self._dispatcher = dispatcher

    async def dispatch(self, request: dict) -> dict:
        amr_id = request.get("amr_id")
        destination = request.get("destination_node_id")
        sim_time = float(request.get("sim_time") or 0.0)

        if not amr_id or not isinstance(amr_id, str):
            return {
                "job_id": None,
                "status": "amr_not_found",
                "estimated_arrival_s": None,
                "reason": "amr_id is required",
            }
        if not destination or not isinstance(destination, str):
            return {
                "job_id": None,
                "status": "node_not_found",
                "estimated_arrival_s": None,
                "reason": "destination_node_id is required",
            }

        result = await self._dispatcher.dispatch(amr_id, destination, sim_time)
        payload = asdict(result)
        return {
            "job_id": payload["job_id"],
            "status": payload["status"],
            "estimated_arrival_s": payload["estimated_arrival_s"],
            "reason": payload["reason"],
        }
