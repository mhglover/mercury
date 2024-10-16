#!/usr/bin/env python
"""mercury radio database models"""

from datetime import timezone as tz, datetime as dt, timedelta
import logging
import uuid
from dataclasses import dataclass, field
from typing import List
from humanize import naturaltime
import tekore as tk
from tortoise.models import Model
from tortoise import fields, exceptions
from helpers import feelabout, truncate_middle

ENDZONE_THRESHOLD_MS = 30000  # last thirty seconds of a track
SKIP_THRESHOLD_PERCENTAGE = 80
INSTANCE_ID = str(uuid.uuid4())

class Option(Model):
    """track application options in a database table"""
    id = fields.IntField(primary_key=True)
    option_name = fields.TextField()
    option_value = fields.TextField(null=True)

    def __str__(self):
        return str(self.option_name)
    
    
class User(Model):
    """track users"""
    id = fields.IntField(primary_key=True)
    spotifyid = fields.TextField()
    displayname = fields.TextField()
    token = fields.BinaryField()
    last_active = fields.DatetimeField(auto_now=True)
    status = fields.TextField()
    watcherid = fields.TextField(null=True)
    role = fields.TextField(default="user")
    ratings: fields.ReverseRelation["Rating"] = []
    websocket = None


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
    reason = fields.TextField(null=True)
    track: fields.ForeignKeyRelation[Track] = fields.ForeignKeyField(
        "models.Track", related_name="histories")
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="histories")
    rating: fields.ForeignKeyRelation[Rating] = fields.ForeignKeyField(
        "models.Rating", related_name="histories")
    recommendation: fields.ForeignKeyRelation["Recommendation"] = fields.ForeignKeyField(
        "models.Recommendation", related_name="histories")

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
    trackname: str = ""
    template_id: str = ""
    track_id: int = 0
    color: str = ""
    rating: int = 0
    comment: str = ""
    timestamp: str = ""
    reason: str = ""
    listeners: list = field(default_factory=list)

    def to_dict(self):
        """Convert to dict with custom serialization"""
        return {
            "trackname": self.trackname,
            "template_id": f"track_{self.track_id}",
            "track_id": self.track_id,
            "color": self.color,
            "rating": self.rating,
            "comment": self.comment,
            "timestamp": self.timestamp,
            "listeners": self.listeners,
            "reason": self.reason
        }

@dataclass
class WebUser():
    """data model for passing track data to a web template"""
    displayname: str = None
    user_id: int = None
    color: str = None
    rating: int = None
    track_id: int = None
    trackname: str = None
    role: str = None
    comment: str = None


@dataclass
class WebData():
    """data model for passing state to web template"""
    track: WebTrack = field(default_factory=WebTrack)
    tracks: List[WebTrack] = field(default_factory=list)
    history: List[WebTrack] = field(default_factory=list)
    user: User = field(default_factory=User)
    users: List[WebUser] = field(default_factory=list)
    ratings: List[Rating] = field(default_factory=list)
    nextup: WebTrack = field(default_factory=WebTrack)
    
    redirect_url: str = None
    refresh: int = 60
    currently: tk.model.CurrentlyPlaying = None

    def to_dict(self):
        """Convert to dict with custom serialization"""
        now = dt.now(tz.utc)
        return {
            "user": {"id": self.user.id,
                     "displayname": self.user.displayname,
                     "spotifyid": self.user.spotifyid,
                     "status": self.user.status},
            "ratings": [{ "color": feelabout(rating.rating),
                          "trackname": rating.trackname,
                          "rating": rating.rating,
                          "displayname": rating.user.displayname,
                          "comment": rating.comment,
                          "userid": rating.user.id,
                          "last_played": naturaltime(now - rating.last_played)
                        } for rating in self.ratings],
            "history": self.history,
            "users": {user.user_id: { "displayname": user.displayname,
                                      "user_id": user.user_id,
                                      "color": user.color,
                                      "rating": user.rating,
                                      "comment": user.comment,
                                      "track_id": user.track_id,
                                      "trackname": user.trackname
                                      } for user in self.users},
            "nextup": self.nextup.to_dict(),
            "refresh": self.refresh,
            "track": (self.track.to_dict()),
            
        }

@dataclass
class WatcherState():
    """hold the state of a spotify watcher"""
    
    cred: tk.Credentials
    spotify: tk.Spotify
    user: User = field(default_factory=User)
    token: tk.Token = None
    status: str = "unknown"
    sleep: int = 0
    currently: tk.model.CurrentlyPlaying = None
    nextup: Recommendation = field(default_factory=Recommendation)
    
    track: Track = field(default_factory=Track)
    reason: str = ""
    rating: Rating = field(default_factory=Rating)
    history: PlayHistory = field(default_factory=PlayHistory)
    displaytime: str = ""
    is_saved: str = None
    position: int = 0
    just_rated: bool = False
    endzone: str = None
    finished: bool = False
    remaining_ms: int = 0
    
    track_last_cycle: Track = field(default_factory=Track)
    position_last_cycle: int = 0
    was_saved_last_cycle: str = None

    
    def __post_init__(self):
        """timeout if they stop playing"""
        now = dt.now(tz.utc)
        self.ttl = now + timedelta(minutes=20)
                # set the watcherid for the spotwatcher process
    
    def refresh_token(self):
        if self.token.is_expiring:
            try:
                self.token = self.cred.refresh(self.token)
            except tk.BadRequest as e:
                logging.error("refresh_token - %s", e)
                self.token = None
            except Exception as e:
                logging.error("refresh_token - %s", e)
                self.token = None
    
    async def set_watcher_name(self):
        self.user.watcherid = (f"watcher_{self.user.spotifyid}_" 
                                + f"{dt.now(tz.utc)}")
        await self.user.save()

    async def refresh(self):
        self.user = await User.get(id=self.user.id)
        self.refresh_token()
        self.ttl = dt.now(tz.utc) + timedelta(minutes=20)
        self.position = int((self.currently.progress_ms/self.currently.item.duration_ms) * 100)
        self.remaining_ms = self.currently.item.duration_ms - self.currently.progress_ms
        self.displaytime = "{:}:{:02}".format(*divmod(self.remaining_ms // 1000, 60)) 
        self.calculate_sleep_duration()
        self.update_endzone_status()
        logging.debug("updating ttl, last_active and status: %s", naturaltime(self.ttl))
        self.user.last_active = dt.now(tz.utc)
        await self.user.save()
        # update follower ttls
        logging.debug("updating follower ttls")
        followers = await User.filter(watcherid=self.user.id)
        for each in followers:
            each.last_active = dt.now(tz.utc)
            await each.save()
            logging.debug("updated follower ttl for %s", each.displayname)
        

    def l(self):
        """return a middle-truncated name for the previous track"""
        if not self.track_last_cycle.id:
            return None
        return str(truncate_middle(self.track_last_cycle.trackname))

    def t(self):
        """return a middle-truncated track name"""
        return str(truncate_middle(self.track.trackname))
    
    def n(self):
        """return a middle-truncated name for the nextup track"""
        return str(truncate_middle(self.nextup.trackname))

    def update_endzone_status(self):
        self.endzone = self.remaining_ms <= ENDZONE_THRESHOLD_MS

    def track_changed(self) -> bool:
        return not self.track_last_cycle.id == self.track.id
    
    def savestate_changed(self) -> bool:
        return ( self.was_saved_last_cycle is not None and 
                 not self.track_changed() and 
                 self.was_saved_last_cycle != self.is_saved)

    def calculate_sleep_duration(self):
        
        min_sleep_duration = 1  # Minimum sleep duration in seconds
        max_sleep_duration = 30  # Maximum sleep duration in seconds

        if self.status == "active":
            # if we're in the last thirty seconds of a track, sleep for half the remaining time
            if self.remaining_ms < ENDZONE_THRESHOLD_MS:
                sleep_duration = self.remaining_ms / 2 / 1000
                
            # if we're within 30 seconds of the end, sleep until the 30 second mark
            elif (self.remaining_ms - ENDZONE_THRESHOLD_MS) < ENDZONE_THRESHOLD_MS:
                sleep_duration = (self.remaining_ms - ENDZONE_THRESHOLD_MS) / 1000
            
            # if we're in the first 30 seconds of a track, sleep for 3 seconds
            elif self.currently.progress_ms < 30000:
                sleep_duration = 3
                
            # otherwise we should be in the middle, sleep for 30 seconds
            else:
                sleep_duration = 30
        else:
            sleep_duration = 30

        # Ensure the sleep duration is at least the minimum sleep duration
        self.sleep = max(sleep_duration, min_sleep_duration)
        logging.debug("position: %s sleep duration: %s", self.position, self.sleep)

    def was_skipped(self):
        # if the last position we saw was less than 80% through, consider it a skip
        return self.position_last_cycle < SKIP_THRESHOLD_PERCENTAGE

    def next_is_now_playing(self):
        if self.nextup is None:
            return False
        result = (self.nextup and 
                  self.track.id == self.nextup.track.id)
        logging.debug("next_is_nowplaying? %s %s ? %s", result, self.track.id, self.nextup.track.id)
        return result
    
    def next_has_expiration(self):
        result = (self.nextup and 
                  self.nextup.expires_at is not None and 
                  self.nextup.expires_at != '')
        logging.info("next_has_expiration? %s: %s", result, self.nextup.expires_at )
        return result
    
    async def cleanup(self):
        self.user.watcherid = ""
        self.user.status = "inactive"
        await self.user.save()
        
        followers = await User.filter(watcherid=self.user.id)
        for f in followers:
            f.watcherid = ""
            f.status = "inactive"
            await f.save()


class Lock(Model):
    lock_name = fields.CharField(max_length=255, pk=True)
    acquired_at = fields.DatetimeField(auto_now_add=True)
    instance = fields.CharField(max_length=255, default=INSTANCE_ID)

    class Meta:
        table_name = "locks"
    
    @classmethod
    async def attempt_acquire_lock(cls, lock_name: str) -> bool:
        """create a lock record if it doesn't exist, update if it's from this instance"""
        try:
            # if the lock exists from another instance, return False
            lock = await cls.get_or_none(lock_name=lock_name)
            # if the lock doesn't exist, create it and return True
            if lock is None:
                await cls.create(lock_name=lock_name, instance=INSTANCE_ID)
                return True
            
            # if a lock from this instance already exists, return True
            if lock.instance == INSTANCE_ID:
                logging.debug(
                    "attempt_acquire_lock - lock %s already exists from this instance: %s",
                    lock_name, INSTANCE_ID)
                return True
            
            logging.warning(
                "attempt_acquire_lock - lock %s already exists from another instance: %s",
                lock_name, lock.instance)
            return False
            
        except exceptions.IntegrityError:
            # If lock already exists, return False
            return False
        
    @classmethod
    async def check_for_lock(cls, lock_name: str) -> bool:
        await cls.exists(lock_name=lock_name, instance=INSTANCE_ID)

    @classmethod
    async def release_lock(cls, lock_name: str) -> None:
        # Delete the lock record
        await cls.filter(lock_name=lock_name, instance=INSTANCE_ID).delete()
    
    @classmethod
    async def release_all_user_locks(cls, user_id: str) -> None:
        # Delete the lock record
        await cls.filter(lock_name=user_id).delete()
    
    @classmethod
    async def release_all_locks(cls):
        # Delete ALL the lock records muhahahaha
        await cls.filter().delete()
    