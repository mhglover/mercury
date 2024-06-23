#!/usr/bin/env python
"""mercury radio database models"""

from dataclasses import dataclass
from typing import List
from tortoise import fields
from tortoise.models import Model

# pylint: disable=trailing-whitespace

class User(Model):
    """track users"""
    id = fields.IntField(primary_key=True)
    spotifyid = fields.TextField()
    displayname = fields.TextField()
    token = fields.BinaryField()
    last_active = fields.DatetimeField(auto_now=True)
    status = fields.TextField()
    watcherid = fields.TextField()
    role = fields.TextField(default="user")
    
    ratings: fields.ReverseRelation["Rating"]

    def __str__(self):
        return str(self.spotifyid)


class Track(Model):
    """track tracks"""
    id = fields.IntField(primary_key=True)
    spotifyid = fields.CharField(max_length=255)
    trackname = fields.TextField()
    trackuri = fields.CharField(max_length=255)
    duration_ms = fields.IntField()
    
    ratings: fields.ReverseRelation["Rating"]
    histories: fields.ReverseRelation["PlayHistory"]

    def __str__(self):
        return str(self.trackname)


class Rating(Model):
    """track likes"""
    id = fields.IntField(primary_key=True)
    trackname = fields.TextField()
    rating = fields.IntField()
    last_played = fields.DatetimeField()
    comment = fields.TextField(null=True)
    
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="ratings" )
    track: fields.ForeignKeyRelation[Track] = fields.ForeignKeyField(
        "models.Track", related_name="ratings")

    def __str__(self):
        return str(self.trackname)


class PlayHistory(Model):
    """track the history of songs played"""
    id = fields.IntField(primary_key=True)
    played_at = fields.DatetimeField(auto_now=True)
    trackname = fields.TextField()
    track: fields.ForeignKeyRelation[Track] = fields.ForeignKeyField(
        "models.Track", related_name="histories")

    def __str__(self):
        return str(self.trackname)


class Recommendation(Model):
    """track the recommended tracks"""
    id = fields.IntField(primary_key=True)
    track: fields.ForeignKeyRelation[Track] = fields.ForeignKeyField(
        "models.Track", related_name="recommendations")
    trackname = fields.TextField()
    queued_at = fields.DatetimeField(auto_now=True)
    expires_at = fields.DatetimeField(null=True)
    reason = fields.TextField(null=True)

    def __str__(self):
        return str(self.trackname)


@dataclass
class WebTrack():
    """data model for passing track data to a web template"""
    trackname: str
    track_id: int
    color: str
    rating: int


@dataclass
class WebData():
    """data model for passing state to web template"""
    history: List[PlayHistory]
    activeusers: List[User]
    ratings: List[WebTrack]
    nextup: Recommendation
    user: User = None
    track: Track = None
    redirect_url: str = None

    def to_dict(self):
        """Convert to dict with custom serialization for datetime"""
        return {
            "user": {"displayname": self.user.displayname,
                     "spotifyid": self.user.spotifyid},
            "ratings": {track.track_id: {"color": track.color,
                                         "trackname": track.trackname,
                                         "rating": track.rating}
                            for track in self.ratings},
            "history": list(set(x.trackname for x in self.history)),
            "playing_trackname": self.track.trackname,
            "activeusers": [x.displayname for x in self.activeusers],
            "nextup_trackname": self.nextup.trackname,
        }
