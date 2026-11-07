from django.db import models


class PendingSignup(models.Model):
    """A signup waiting for its email link. Nothing else exists until the link is used."""

    email = models.EmailField(unique=True)
    tenant_name = models.CharField(max_length=200)
    slug = models.CharField(max_length=200)
    password = models.CharField(max_length=128)  # Already hashed.
    token_hash = models.CharField(max_length=64, unique=True)  # The link itself is never stored.
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.slug} for {self.email}"
