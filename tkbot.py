# from distutils.log import error
from multiprocessing import set_forkserver_preload
import signal
import sys
from collections import deque
from hikari import ExceptionEvent
import tekore as tk
from random import choice
import asyncio
import os
from dotenv import load_dotenv
import logging
import asyncio
from quart import Quart, request, redirect
from peewee import *
import time
from playhouse.db_url import connect
import pickle
from datetime import datetime, timedelta
import nextcord
from nextcord.ext import commands

pgdb = connect(os.environ['DATABASE_URL'], autorollback=True)

app = Quart(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

load_dotenv()  # take environment variables from .env

CHANNEL = os.environ['DISCORD_CHANNEL']
SERVER = os.environ['DISCORD_SERVER']
PREFIX=os.environ['DISCORD_COMMAND_PREFIX']
USER=os.environ['USER']
PORT=os.environ['PORT']
discord_token = os.environ['DISCORD_TOKEN']

conf = tk.config_from_environment()
cred = tk.Credentials(*conf)
token_spotify = tk.request_client_token(*conf[:2])

description = "Spotify track search bot using Tekore"
bot = commands.Bot(command_prefix=PREFIX, description=description, activity=nextcord.Game(name=f"loading..."))
spotify = tk.Spotify(token_spotify, asynchronous=True)
queue = deque()
recommendations = []

class BaseModel(Model):
    """A base model that will use our Postgresql database"""
    class Meta:
        database = pgdb


class User(BaseModel):
    id = TextField(primary_key=True)
    token = BlobField()
    email = TextField()
    display_name = TextField()
    href = TextField()


class Rating(BaseModel):
    user_id = ForeignKeyField(User, backref="ratings")
    trackid = CharField()
    rating = IntegerField()
    last_played = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])

# class Track(BaseModel):
#     id = TextField(primary_key=True)

class PlayHistory(BaseModel):
    trackid = CharField()
    played_at = TimestampField()

    class Meta:
        indexes = (
            (('trackid', 'played_at'), True),
        )


def sigterm_handler(signal, frame):
    # save the state here or do whatever you want
    logging.error(f'caught SIGTERM, exiting...')
    sys.exit(0)

signal.signal(signal.SIGTERM, sigterm_handler)


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():
    code = request.args.get('code', "")
    logging.info(f"callback for code {code}")
    state = request.args.get('state', "")
    auth = auths.pop(state, None)
    spotifyid = users.pop(state, None) 

    if auth is None:
        return 'Invalid state!', 400

    token = auth.request_token(code, state)
    p = pickle.dumps(token)
    user = User.create(id=spotifyid, token=p)
    users[spotifyid] = user
    return redirect(f"https://discord.com/channels/{SERVER}/{CHANNEL}", 307)


@app.before_serving
async def before_serving():
    logging.info("before_serving")


@bot.event
async def on_ready():
    tasks.append(bot.loop.create_task(queue_manager(), name="queue_manager"))
    tasks.append(bot.loop.create_task(spotify_watcher(USER), name=f"{USER}_watcher"))
    logging.info("Bot Ready!")


@bot.slash_command(description="check what's currently playing", guild_ids=[int(SERVER)])
async def np(interaction: nextcord.Interaction):
    userid = str(interaction.user.id)
    user, token = await getuser(userid)
    logging.info(f"checking now playing for {userid}")
    
        
    with spotify.token_as(token):
        playback = await spotify.playback_currently_playing()
        if playback is None:
            await interaction.send("Nothing.")
        else:
            artist = " & ".join([x.name for x in playback.item.artists])
            name = playback.item.name
            milliseconds = playback.item.duration_ms - playback.progress_ms
            seconds = int(milliseconds / 1000) % 60
            minutes = int(milliseconds / (1000*60)) % 60
            await interaction.send(f"{artist} - {name} ({minutes}:{seconds:02} remaining)")


@bot.slash_command(description="up next", guild_ids=[int(SERVER)])
async def upnext(interaction: nextcord.Interaction):
    userid = str(interaction.user.id)
    user, token = await getuser(userid)
        
    with spotify.token_as(token):
        tid = queue[0]
        track_name = await trackinfo(tid)
        await interaction.send(f"{track_name}")


@bot.slash_command(description="search for a track", guild_ids=[int(SERVER)])
async def search(ctx: nextcord.Interaction, *, query: str = None):
    uid = str(ctx.user.id)
        
    tracks, = await spotify.search(query, limit=5)
    embed = nextcord.Embed(title="Track search results", color=0x1DB954)
    embed.set_thumbnail(url="https://i.imgur.com/890YSn2.png")
    embed.set_footer(text="Requested by " + ctx.user.display_name)

    for t in tracks.items:
        artist = t.artists[0].name
        url = t.external_urls["spotify"]

        message = "\n".join([
            "[Spotify](" + url + ")",
            ":busts_in_silhouette: " + artist,
            ":cd: " + t.album.name
        ])
        embed.add_field(name=t.name, value=message, inline=False)

    await ctx.send(embed=embed)


@bot.slash_command(description="rate the currently playing track)", guild_ids=[int(SERVER)])
async def ratethis(ctx: nextcord.Interaction):
    userid = str(ctx.user.id)
    value = 2
    user, token = await getuser(userid)
    with spotify.token_as(token):
        playback = await spotify.playback_currently_playing()
        artist = " & ".join([x.name for x in playback.item.artists])
        name = playback.item.name

        logging.info(f"slash_command {userid} rate {artist} - {name}")
        await rate(userid, playback.item.id, set_last_played=False, value=value)
        await ctx.channel.send(f"rated {artist} - {name} {value}")


@bot.slash_command(description="veto the upcoming track)", guild_ids=[int(SERVER)])
async def veto(ctx: nextcord.Interaction):
    userid = str(ctx.user.id)
    user, token = await getuser(userid)
    vetoed = queue.popleft()
    name, track = await trackinfo(vetoed, return_track=True)
    duration = track.duration_ms
    seconds = int(duration / 1000) % 60
    minutes = int(duration / (1000*60)) % 60
    logging.info(f"{userid}_watcher vetoed {name} ({minutes}:{seconds:02})")
    await ctx.channel.send(f"vetoing {name}, it makes the customers all moshy")


@bot.slash_command(description="listen with us (and grant bot permission to mess with your spoglify)", guild_ids=[int(SERVER)])
async def spotme(ctx: nextcord.Interaction):
    userid = str(ctx.user.id)
    
    if userid in users:
        # user, token = await getuser(userid)
        tasknames = [x.get_name() for x in tasks]

        if userid + "_watcher" not in tasknames:
            logging.info(f"adding a spot watcher for {userid}")
            tasks.append(bot.loop.create_task(spotify_watcher(userid), name=userid + "_watcher"))
        else:
            await ctx.channel.send(f"I already have a watcher for {userid}.")

    else:
        scope = [ "user-read-playback-state",
                "user-modify-playback-state",
                "user-read-currently-playing",
                "user-read-recently-played",
                "streaming",
                "app-remote-control",
                "user-library-read",
                "user-top-read",
                "playlist-read-private",
                ]
        auth = tk.UserAuth(cred, scope)

        auths[auth.state] = auth
        users[auth.state] = userid

        # Returns: 1
        channel = await ctx.user.create_dm()
        logging.info(f"Attempting authentication for {ctx.user.name}")
        await channel.send(f"To grant the bot access to your Spotify account, click here: {auth.url}")


@bot.slash_command(description="dig into your spotify to find out what you like", guild_ids=[int(SERVER)])
async def pullratings(interaction: nextcord.Interaction):
    userid = str(interaction.user.id)
    user, token = await getuser(userid)

    with spotify.token_as(token):
        try:
            ratings = Rating.get(user_id=userid)
        except Exception as e:
            logging.error(f"rating error: {e}")
        
        # rate history (20 items)
        r = [item.track.id async for item in spotify.all_items(await spotify.playback_recently_played())]
        recents = await spotify.playback_recently_played()
        rated = await rate_list(recents.items, userid, 1)
        
        # rate tops
        tops = [x async for x in spotify.all_items(await spotify.current_user_top_tracks())]
        rated = rated + await rate_list(tops, userid, 4)

        s = await spotify.saved_tracks()
        saved_tracks = [item.track.id async for item in spotify.all_items(await spotify.saved_tracks())]
        rated = rated + await rate_list(saved_tracks, userid, 4)

        message = f"rated {rated} items"
        if interaction.is_expired():
            await interaction.channel.send(message)
        else:
            await interaction.send(message)


async def getuser(userid):
    user = User.get(User.id == userid)
    token = pickle.loads(user.token)
    if token.is_expiring:
        token = cred.refresh(token)
        User.token = token
    
    return user, token


async def trackinfo(trackid, return_track=False):
    """pull track name (optionally also track object)

    Args:
        trackid (str): Spotify's unique track id
        return_track (bool, optional): also return the track. Defaults to False.

    Returns:
        str: track artist and title
        str, track object: track artist and title, track object
    """
    try:
        track = await spotify.track(trackid)
    except Exception as e:
        logging.error(f"-------------- couldn't retrieve track from spotify: {trackid}")
    artist = " & ".join([x.name for x in track.artists])
    name = f"{artist} - {track.name}" 

    if return_track is True:
        return name, track
    else:
        return name


async def rate_list(items, uid, rating):
    if type(items) is list:
        if type(items[0]) is str:
            trackids = items
        else:
            trackids = [x.id for x in items]
    else:
        trackids = [x.track.id for x in items]
    logging.debug(f"rating {len(trackids)} tracks")
    tracks = [{"trackid": i, "user_id": uid, "rating": rating, "set_last_played": False} for i in trackids]

    with pgdb.atomic():
        for each in tracks:
            logging.debug(f"rating {each['user_id']} {each['trackid']} {rating}")
            try:
                Rating.get_or_create(**each)
            except Exception as e:
                logging.error(f"rating history: {e}")
    
    return len(tracks)


async def rate(uid, tid, value=1, set_last_played=True):
    trackname = await trackinfo(tid)
    logging.info(f"{uid}_watcher rating {trackname} {value}")
    try: 
        if set_last_played:        
            rating = Rating.get_or_create(user_id=uid, trackid=tid, rating=value)
        else:
            rating = Rating.get_or_create(user_id=uid, trackid=tid, rating=value, last_played="1970-01-01")
    except Exception as e:
        logging.error(f"{uid}_watcher rating error: {e}")


async def history(uid, tid):
    trackname= await trackinfo(tid)
    logging.info(f"{uid}_watcher added playhistory for {trackname}")
    try:
        insertedkey = PlayHistory.get_or_create(trackid=tid, played_at=time.time())
    except Exception as e:
        logging.error(f"{uid} couldn't get/create history: {e}")


async def spotify_watcher(userid):
    global recommendations
    procname = f"{userid}_watcher"
    logging.info(f"{procname} starting")
    user, token = await getuser(userid)
    playing_tid = ""

    with spotify.token_as(token):
        currently = await spotify.playback_currently_playing()
        if currently is None:
            logging.info(f"{procname} not currently playing")
            sleep = 30
        else:
            previous_tid = None
            playing_tid = currently.item.id
            trackname = await trackinfo(playing_tid)
            nextup_tid = queue[0]
            
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60
            logging.info(f"{procname} playing {trackname} {minutes}:{seconds:02}s remaining")
            
            logging.info(f"{procname} pulling spotify recommendations")
            r = await spotify.recommendations(track_ids=[playing_tid])
            recommendations += [item.id for item in r.tracks]
            logging.info(f"{procname} found {len(recommendations)} recommendations")

            try:
                activity = nextcord.Activity(name=f"{trackname}", url="urlhere", type=nextcord.ActivityType.listening)
                await bot.change_presence(status=nextcord.Status.idle, activity=activity)
            except Exception as e:
                logging.error(f"{procname} error setting discord status: {e}")
            
            logging.info(f"{procname} set the nowplaying message: {trackname}")

    
    while True:
        logging.debug(f"{userid}_watcher awake")

        with spotify.token_as(token):
            currently = await spotify.playback_currently_playing()
            if currently is None:
                logging.info(f"not currently playing")
                return
            
            trackid = currently.item.id
            trackname = await trackinfo(trackid)
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60

            nextup_tid = queue[0]
            nextup_name = await trackinfo(nextup_tid)

            # do logic to see if lastplayed finished?
            # logging.info(f"{procname} started playing {trackname} {remaining_ms/1000}s remaining")

            if trackid == nextup_tid:
                logging.info(f"{procname} popping queue, next up is same as currently playing: {nextup_name}")
                queue.popleft()
                await history(userid, trackid)
                try:
                    activity = nextcord.Activity(name=f"up next: {nextup_name}", url="urltest", type=nextcord.ActivityType.streaming)
                    await bot.change_presence(status=nextcord.Status.idle, activity=activity)
                except Exception as e:
                    logging.info(f"{procname} couldn't set presence: {e}")

            if remaining_ms > 30000:
                if (remaining_ms - 30000) < 30000:
                    sleep = (remaining_ms - 30000) / 1000
                else:
                    sleep = 30

            elif remaining_ms <= 30000:                
                await rate(userid, trackid, 1)                                
                try:
                    logging.info(f"{procname} sending to spotify client queue {nextup_name}")
                    track = await spotify.track(nextup_tid)
                    result = await spotify.playback_queue_add(track.uri)
                    
                    sleep = (remaining_ms /1000) + 1
                except Exception as e:
                    logging.error(f"{procname} queuing error: {e}\n\n{result}")
                    sleep = 5
                
                
                logging.debug(f"{procname}  ")
        logging.debug(f"{procname} playing {trackname} {minutes}:{seconds:02}s remaining, sleeping for {sleep}s")
        await asyncio.sleep(sleep)
        #TODO add a ttl countdown somehow
    

async def queue_manager():
    global users
    procname = "queue_manager"
    logging.info(f'{procname} starting')
    ttl = {}
    if len(users) == 0:
        users = [USER]

    while True:
    
        while len(queue) < 2:
            timeout = SQL("current_timestamp - INTERVAL '1 minute'")
            ratings = Rating.select(Rating.trackid).where(Rating.last_played < timeout).group_by(Rating.trackid).where(Rating.user_id in [x for x in users])
            history = [x.trackid for x in PlayHistory.select()]
            potentials = [x.trackid for x in ratings if x.trackid not in history]
            
            for each in recommendations:
                if each not in potentials:
                    potentials.append(each)
            
            upcoming_tid =  choice(potentials)
            result = await trackinfo(upcoming_tid, return_track=True)
            (upcoming_name, upcoming_track) = result
            
            queue.append(upcoming_tid)
            
            ttl[upcoming_tid] = time.time() + (upcoming_track.duration_ms/1000)
            h = [x for x in PlayHistory.select(PlayHistory.played_at).where(PlayHistory.trackid == upcoming_tid)]
            r = [x for x in Rating.select(Rating.rating).where(Rating.trackid == upcoming_tid)]
            if len(h) == 0:
                h = "fresh"
            if len(r) == 0:
                r = "fresh"
            else:
                r = sum([x.rating for x in r])


            logging.info(f"{procname} queued: {upcoming_name} [{r} - {h}] of {len(potentials)} potential songs")
            

        await asyncio.sleep(10)


if __name__ == "__main__":
    pgdb.connect()
    pgdb.create_tables([User, Rating, PlayHistory])

    auths = {}  # Ongoing authorisations: state -> UserAuth
    # users = {}  # User tokens: state -> token (use state as a user ID)
    users = {}
    tasks = []
        
    bot.loop.create_task(app.run_task('0.0.0.0', PORT))
    bot.run(discord_token)
    
