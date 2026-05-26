"""Small misc endpoints to support the Help page."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/help", tags=["help"])


@router.get("/example.csv", response_class=PlainTextResponse)
async def example_csv() -> PlainTextResponse:
    body = (
        "prompt,ref1,ref2,ref3,ref4,width,height,seed,negative_prompt,enhance,name\n"
        '"a wide cinematic shot of a misty forest at dawn",,,,,1024,1024,random,,true,forest_dawn\n'
        '"a robot kitten sitting on the same forest log","C:/refs/kitten.png",,,,1024,1024,42,,true,robokitten\n'
        '"the same kitten, now leaping","C:/refs/kitten.png","C:/refs/forest.png",,,1024,1024,random,,true,leap\n'
    )
    return PlainTextResponse(body, headers={"Content-Disposition": 'attachment; filename="batch_example.csv"'})
