import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, mq, outbox
from app.database import get_session
from app.schemas import EnrollRequestIn, EnrollRequestOut, StatusOut

router = APIRouter()


@router.post("/courses/{course_id}/enroll", response_model=EnrollRequestOut, status_code=202)
async def enroll(
    course_id: int, body: EnrollRequestIn, session: AsyncSession = Depends(get_session)
) -> EnrollRequestOut:
    req = await db.create_enrollment_request(session, body.student_id, course_id)
    await session.commit()

    published = await mq.publish_request(req.id)
    if not published:
        outbox.spool_write(req.id, {"student_id": body.student_id, "course_id": course_id})

    return EnrollRequestOut(request_id=req.id)


@router.get("/enrollments/requests/{request_id}", response_model=StatusOut)
async def get_status(request_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> StatusOut:
    req = await db.get_enrollment_request(session, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")

    return StatusOut(request_id=req.id, student_id=req.student_id, course_id=req.course_id, status=req.status)
