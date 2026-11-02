"""
Корневой роутер.

Каждый подроутер получает свой листовой префикс, как в EDMS, но без её
главной ловушки: там `documents_router` содержал catch-all `/{file_name:path}`
и его приходилось подключать последним, иначе он перехватывал запросы ко всем
остальным префиксам. Здесь catch-all маршрутов нет, порядок подключения
значения не имеет.
"""

from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.memos import router as memos_router
from app.api.routes.public_sign import router as public_sign_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth")
router.include_router(memos_router, prefix="/memos")
router.include_router(admin_router, prefix="/admin")
# Без авторизации: доступ только по одноразовому токену.
router.include_router(public_sign_router, prefix="/public")
