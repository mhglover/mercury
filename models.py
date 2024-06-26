#!/usr/bin/env python
"""mercury radio database models"""

import datetime
from dataclasses import dataclass, field
from typing import List
import tekore as tk
from tortoise import fields
from tortoise.models import Model
from helpers import truncate_middle

# pylint: disable=trailing-whitespace, trailing-newlines

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
    """Tracks model"""
    id = fields.IntField(primary_key=True)
    trackname = fields.TextField()
    trackuri = fields.CharField(max_length=255)
    duration_ms = fields.IntField()
    spotifyid = fields.TextField(null=True)
    
    ratings: fields.ReverseRelation["Rating"]
    histories: fields.ReverseRelation["PlayHistory"]
    spotifyids: fields.ReverseRelation["SpotifyID"]

    def __str__(self):
        return str(self.trackname)


class SpotifyID(Model):
    """Spotify ID model"""
    id = fields.IntField(primary_key=True)
    spotifyid = fields.CharField(max_length=255)
    track = fields.ForeignKeyField('models.Track', related_name='spotifyids')

    def __str__(self):
        return self.spotifyid


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
class WebUser():
    """data model for passing track data to a web template"""
    displayname: str = None
    user_id: int = None
    color: str = None
    rating: int = None
    track_id: int = None
    trackname: str = None


@dataclass
class WebData():
    """data model for passing state to web template"""
    history: List[PlayHistory]
    users: List[WebUser]
    ratings: List[WebTrack]
    nextup: Recommendation
    user: User = User()
    track: Track = Track()
    redirect_url: str = None
    refresh: int = 60

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
            "users": self.users,
            "nextup_trackname": self.nextup.trackname,
            "refresh": self.refresh
        }
        
@dataclass
class WatcherState(): # pylint: disable=too-many-instance-attributes
    """hold the state of a spotify watcher"""
    
    cred: tk.Credentials
    user: User = field(default_factory=User)
    token: tk.AccessToken = None
    
    currently: tk.model.CurrentlyPlaying = None
    track: Track = field(default_factory=Track)
    last_track: Track = field(default_factory=Track)
    nextup: Track = field(default_factory=Track)
    
    displaytime: str = ""
    position: int = 0
    last_position: int = 0
    sleep: int = 30
    
    rated: str = None
    is_this_saved: str = None
    was_saved: str = None
    endzone: str = None
    
    def __post_init__(self):
        # timeout if they stop playing
        now = datetime.datetime.now(datetime.timezone.utc)
        self.ttl = now + datetime.timedelta(minutes=20)
        
    def t(self):
        """return a middle-truncated track name"""
        return str(truncate_middle(self.track.trackname))
    
    def n(self):
        """return a middle-truncated name for the nextup track"""
        return str(truncate_middle(self.nextup.trackname))

