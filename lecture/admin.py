from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, Lecture, PdfChunk, Mapping

# 커스텀 User 모델을 admin에 등록
@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    pass

# 다른 모델들도 등록
admin.site.register(Lecture)
admin.site.register(PdfChunk)
admin.site.register(Mapping)
