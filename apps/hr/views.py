from django.http import HttpResponse
def index(request):
    return HttpResponse("HR app OK")