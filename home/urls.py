from django.urls import path
from . import views
from django.contrib.auth import views as auth_views

urlpatterns = [
    path("", views.home, name="home"),   # 👈 now homepage
    path("upload/", views.upload_document, name="upload_document"),
    path("documents/", views.document_list, name="document_list"),
    path('score/<int:doc_id>/', views.score_cv, name='score_cv'),
    path('signup/', views.signup_view, name='signup'),
    path('login/', auth_views.LoginView.as_view(template_name='home/login.html'), name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('initiate_payment/', views.initiate_payment, name='initiate_payment'),
    path('verify_payment/', views.verify_payment, name='verify_payment'),
]

