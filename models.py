#!/usr/bin/env python
"""mercury radio database models"""

from tortoise import fields
from tortoise.models import Model

# pylint: disable=trailing-whitespace

class User(Model):
    """track users"""
    spotifyid = fields.TextField()
    displayname = fields.TextField()
    token = fields.BinaryField()
    last_active = fields.DatetimeField(auto_now=True)
    status = fields.TextField()

    def __str__(self):
        return str(self.spotifyid)


class Track(Model):
    """track tracks"""
    trackid = fields.CharField(max_length=255)
    trackname = fields.TextField()
    trackuri = fields.CharField(max_length=255)
    duration_ms = fields.IntField()

    def __str__(self):
        return str(self.trackname)


class Rating(Model):
    """track likes"""
    userid = fields.TextField()
    trackid = fields.TextField()
    trackname = fields.TextField()
    rating = fields.IntField()
    last_played = fields.DatetimeField(auto_now=True)

    def __str__(self):
        return str(self.trackname)


class PlayHistory(Model):
    """track the history of songs played"""
    trackid = fields.TextField()
    played_at = fields.DatetimeField(auto_now=True)

    def __str__(self):
        return str(self.trackid)


class UpcomingQueue(Model):
    """track the upcoming songs"""
    trackid = fields.TextField()
    queued_at = fields.DatetimeField(auto_now=True)

    def __str__(self):
        return str(self.trackid)
