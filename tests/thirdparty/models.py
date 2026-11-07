"""Stands in for an installed library: the package cannot change this code or its migrations."""

import uuid

from django.db import models


class Article(models.Model):
    title = models.CharField(max_length=50)


class ArticleProxy(Article):
    class Meta:
        proxy = True


class Snippet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    body = models.CharField(max_length=50)


class Code(models.Model):
    code = models.CharField(max_length=10, primary_key=True)


class Author(models.Model):
    name = models.CharField(max_length=50)


class Book(models.Model):
    title = models.CharField(max_length=50)
    authors = models.ManyToManyField(Author)
