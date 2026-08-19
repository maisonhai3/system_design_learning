from fastapi import FastAPI

from app.api.enrollments import router as enrollments_router

app = FastAPI(title="Student Course Enrollment")
app.include_router(enrollments_router)
