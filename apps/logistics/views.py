from django.http import HttpResponse
def index(request):
    return HttpResponse("Logistics app OK")