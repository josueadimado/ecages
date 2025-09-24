from django.db import models

class Provider(models.Model):
    name = models.CharField(max_length=200, unique=True)
    contact = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Brand(models.Model):
    """
    Brands are defined per provider.
    Example __str__: "ORG — ORG from ORG" when you add your own naming scheme.
    """
    name = models.CharField(max_length=200)
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="brands")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["provider__name", "name"]
        unique_together = ("provider", "name")

    def __str__(self) -> str:
        # e.g. "ORG from ORG" or include self.pk for your display like "5 – ORG from ORG"
        base = f"{self.name} from {self.provider.name}"
        return f"{self.pk} – {base}" if self.pk else base