from distutils.log import error
from collections import deque
import tekore as tk
from random import choice
import asyncio
import os
from dotenv import load_dotenv
import logging
import asyncio
from quart import Quart, request, redirect
from peewee import *
import psycopg2
from playhouse.db_url import connect
import pickle
from datetime import datetime
import nextcord
from nextcord.ext import commands

pgdb = connect(os.environ['DATABASE_URL'], autorollback=True)

app = Quart(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
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

class BaseModel(Model):
    """A base model that will use our Postgresql database"""
    class Meta:
        database = pgdb

class User(BaseModel):
    id = TextField()
    token = BlobField()
    class Meta:
        database = pgdb

class Rating(BaseModel):
    userid = CharField()
    trackid = CharField()
    rating = IntegerField()

class PlayHistory(BaseModel):
    trackid = CharField()
    played_at = TimestampField()

    class Meta:
        indexes = (
            (('trackid', 'played_at'), True),
        )


@bot.event
async def on_ready():
    tasks.append(bot.loop.create_task(queue_manager(), name="queue_manager"))
    query = User.select()
    for user in query:
        token = pickle.loads(user.token)
        if token.expires_in <= 0:
            token = cred.refresh(token)
        user.token = token
        users[user.id] = user
        
        tasks.append(bot.loop.create_task(spotify_watcher(token, uid=user.id), name=user.id + "_watcher"))
        
    logging.info("Bot Ready!")


@bot.slash_command(description="check what's currently playing", guild_ids=[int(SERVER)])
async def np(interaction: nextcord.Interaction):
    user = users[str(interaction.user.id)]
    token = await getuser(user.id)
    if "queue_manager" in tasks:
        pass
        
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


@bot.slash_command(description="what's next", guild_ids=[int(SERVER)])
async def un(interaction: nextcord.Interaction):
    itoken = interaction.token
    iuser = interaction.user
    uid = str(iuser.id)
    user = users[uid]
        
    with spotify.token_as(token):
        tid = queue[0]
        track_name = await trackinfo(tid)
        await interaction.send(f"{track_name}")


@bot.slash_command(description="search for a track", guild_ids=[int(SERVER)])
async def searchtrack(ctx: nextcord.Interaction, *, query: str = None):
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


@bot.command()
async def devices(ctx, *, query: str = None):
    if str(ctx.author.id) in users:
        user = users[str(ctx.author.id)]
        if type(user.token) == bytes:
            token = pickle.loads(user.token)
        else:
            token = user.token

        if token.is_expiring:
            token = cred.refresh(token)
            user.token = token
            user.save()
            users[user] = token
        
        with spotify.token_as(token):
            devices = await spotify.playback_devices()
            await ctx.send("fetched devices: %s" % [x.name for x in devices])
            return

    else:
        ctx.send("Sorry, you aren't authorized.")
        return


@bot.slash_command(description="grant permission to mess with your spoglify", guild_ids=[int(SERVER)])
async def spotme(ctx: nextcord.Interaction):
    if str(ctx.author.id) in users:
        await ctx.channel.send(f"You're already set up as user {ctx.author.id}! Starting a watcher.")
        bot.loop.create_task(spotify_watcher())
        bot.loop.create_task(queue_manager())

    else:
        # scope = tk.scope.user_read_currently_playing
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
        users[auth.state] = ctx.author.id

        # Returns: 1
        channel = await ctx.author.create_dm()
        logging.info(f"authentication for {ctx.author.name}")
        await channel.send(f"To grant the bot access to your Spotify account, click here: {auth.url}")


@app.before_serving
async def before_serving():
    logging.info("before_serving")


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():

    code = request.args.get('code', "")
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


async def getuser(userid=USER):
    logging.debug(f"fetching user token: {userid}")
    u = User.get(User.id == userid)
    token = pickle.loads(u.token)
    if token.is_expiring:
        token = cred.refresh(token)
    if userid + "_watcher" not in [x.get_name() for x in tasks]:
        tasks.append(bot.loop.create_task(spotify_watcher(token), name=userid + "_watcher"))
    
    return token


async def trackinfo(trackid):
    track = await spotify.track(trackid)
    artist = " & ".join([x.name for x in track.artists])
    name = track.name
    return f"{artist} - {name}"


async def updatehistory(token):
    with spotify.token_as(token):
        history = await spotify.playback_recently_played()
        tracks = spotify.all_items(history)
        dbhistory = [(i.trackid, int(i.played_at.timestamp())) for i in PlayHistory.select()]

        async for played in tracks:
            if (played.track.id, int(played.played_at.timestamp())) not in dbhistory:
                logging.info(f"adding to history: {played.track.id}")
                try:
                    h = PlayHistory(trackid=played.track.id, played_at=int(played.played_at.timestamp()))
                    h.save()
                except Exception as e:
                    logging.exception("tried to write to history and failed with exception")


async def spotify_watcher(token=None, uid=""):
    logging.info(f"starting spotify watcher task for user {uid}")
    if token is None:
        token = await getuser()
    # history = await updatehistory(token)
    ctx = bot.get_channel(CHANNEL)
    # await ctx.send(f"bot connecting to channel")
    
    while True:
        
        with spotify.token_as(token):
            currently = await spotify.playback_currently_playing()
        
        if currently is None:
            # logging.info(f"not currently playing -- attempting to resume")
            return
        else:
            nowplaying = await trackinfo(currently.item.id)
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            if bot is not None:
                await bot.change_presence(activity=nextcord.Game(name=nowplaying))
            logging.debug(f"now playing {nowplaying}, {remaining_ms/1000}s remaining")
        
            if remaining_ms <= 30000:
                logging.debug(f"remaining_ms: {remaining_ms}")
                artist = " & ".join([x.name for x in currently.item.artists])
                name = currently.item.name
                if len(queue) > 0:
                    upcoming_tid = queue.popleft()
                    upcoming_track = await trackinfo(upcoming_tid)
                    track = await spotify.track(upcoming_tid)
                    await bot.change_presence(activity=nextcord.Game(name=upcoming_track))
                    logging.debug(f"queuing {upcoming_track} ({upcoming_tid})")
                    
                    with spotify.token_as(token):
                        logging.debug(f"rating {artist} - {name}")
                        Rating.create(userid=USER, trackid=currently.item.id, rating=1)
                        result = await spotify.playback_queue_add(track.uri)
                        insertedkey = PlayHistory.create(trackid=currently.item.id)
                        logging.debug(f"playhistory inserted: {insertedkey}")
                    sleep = (remaining_ms / 1000) +1
                else:
                    sleep = 0.01
            else:
                sleep = (remaining_ms - 30000 ) / 1000

        logging.debug(f"sleeping for {sleep} seconds")
        await asyncio.sleep(sleep)
    
    await bot.sendMessage("spotify watcher dying")


async def queue_manager():
    logging.info(f'starting queue manager')
    history = []
    tops = []
    recommendations = []
    saveds = []
    potentials = []
    for user in users:
        token = await getuser(user)
        with spotify.token_as(token):
            history = history + [i.trackid for i in PlayHistory.select()]
            tops = tops + [item.id async for item in spotify.all_items(await spotify.current_user_top_tracks())]
            r = await spotify.recommendations(track_ids=[choice(tops)])
            recommendations = recommendations + [item.id for item in r.tracks]
            saveds = saveds + [item.track.id async for item in spotify.all_items(await spotify.saved_tracks())]
            potentials = [x for x in tops + saveds + recommendations if x not in history and x not in queue]
    logging.info(f"{len(potentials)} potential songs")
    
    while True:
        while len(queue) < 1:
                upcoming_tid =  choice(potentials)
                r = await spotify.recommendations(track_ids=[upcoming_tid]) 
                potentials.append(r.tracks[0].id)
                upcoming_track = await trackinfo(upcoming_tid)
                
                queue.append(upcoming_tid)

                logging.debug(f"selected {upcoming_track}")
                # logging.info(f"tops: {len(tops)} saveds: {len(saveds)} history: {len(history)} potentials: {len(potentials)}")

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
    
