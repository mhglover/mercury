# from distutils.log import error
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

class BaseModel(Model):
    """A base model that will use our Postgresql database"""
    class Meta:
        database = pgdb

class User(BaseModel):
    id = TextField(primary_key=True)
    token = BlobField()

class Rating(BaseModel):
    user_id = ForeignKeyField(User, backref="ratings")
    trackid = CharField()
    rating = IntegerField()

class PlayHistory(BaseModel):
    trackid = CharField()
    played_at = TimestampField()

    class Meta:
        indexes = (
            (('trackid', 'played_at'), True),
        )

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
    # query = User.select()
    # for user in query:
    #     users[user.id] = user
        
    #     tasks.append(bot.loop.create_task(spotify_watcher(user.id), name=user.id + "_watcher"))
        
    logging.info("Bot Ready!")


@bot.slash_command(description="check what's currently playing", guild_ids=[int(SERVER)])
async def np(interaction: nextcord.Interaction):
    userid = str(interaction.user.id)
    user = users[userid]
    logging.info(f"checking now playing for {userid}")
    token = await getuser(userid)
        
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
    user = users[str(interaction.user.id)]
    token = await getuser(user.id)
        
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


@bot.slash_command(description="listen with us (and grant bot permission to mess with your spoglify)", guild_ids=[int(SERVER)])
async def spotme(ctx: nextcord.Interaction):
    userid = str(ctx.user.id)
    
    if str(userid) in users:
        token = await getuser(userid)

        if userid + "_watcher" not in [x.get_name() for x in tasks]:
            await ctx.channel.send(f"starting a spot watcher for {userid} ")
            tasks.append(bot.loop.create_task(spotify_watcher(userid), name=userid + "_watcher"))


    else:
        await ctx.channel.send(f"Sending you a DM...")
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
    uid = str(interaction.user.id)
    token = await getuser(uid)

    with spotify.token_as(token):
        try:
            ratings = Rating.get(user_id=uid)
        except Exception as e:
            logging.error(f"rating error: {e}")
        
        # rate history (20 items)
        r = [item.track.id async for item in spotify.all_items(await spotify.playback_recently_played())]
        recents = await spotify.playback_recently_played()
        rated = await rate_list(recents.items, uid, 1)
        
        # rate tops
        tops = [x async for x in spotify.all_items(await spotify.current_user_top_tracks())]
        rated = rated + await rate_list(tops, uid, 4)

        s = await spotify.saved_tracks()
        saved_tracks = [item.track.id async for item in spotify.all_items(await spotify.saved_tracks())]
        rated = rated + await rate_list(saved_tracks, uid, 4)

        message = f"rated {rated} items"
        if interaction.is_expired():
            await interaction.channel.send(message)
        else:
            await interaction.send(message)


async def getuser(userid=None):
    logging.info(f"fetching user token: {userid}")
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


async def rate_list(items, uid, rating):
    if type(items) is list:
        if type(items[0]) is str:
            trackids = items
        else:
            trackids = [x.id for x in items]
    else:
        trackids = [x.track.id for x in items]
    logging.debug(f"rating {len(trackids)} tracks")
    tracks = [{"trackid": i, "user_id": uid, "rating": rating} for i in trackids]

    with pgdb.atomic():
        for each in tracks:
            logging.debug(f"rating {each['user_id']} {each['trackid']} {rating}")
            try:
                Rating.get_or_create(**each)
            except Exception as e:
                logging.error(f"rating history: {e}")
    
    return len(tracks)


async def spotify_watcher(uid):
    logging.info(f"starting spotify watcher task for user {uid}")
    token = await getuser(uid)
    
    while True:
        logging.info("watcher awake")
        
        with spotify.token_as(token):
            logging.info("figuring out current user")
            try:
                user = await spotify.current_user()
            except Exception as e:
                logging.error(f"spotify token error: {e}")
            currently = await spotify.playback_currently_playing()
        
        if currently is None:
            logging.info(f"not currently playing")
            return
        else:
            nowplaying = await trackinfo(currently.item.id)
            remaining_ms = currently.item.duration_ms - currently.progress_ms
        
            if remaining_ms <= 30000:
                logging.debug(f"remaining_ms: {remaining_ms}")
                artist = " & ".join([x.name for x in currently.item.artists])
                name = currently.item.name
                if len(queue) > 0:
                    upcoming_tid = queue.popleft()
                    upcoming_track = await trackinfo(upcoming_tid)
                    track = await spotify.track(upcoming_tid)
                    logging.debug(f"updating presence: UPCOMING {upcoming_track}")
                    await bot.change_presence(activity=nextcord.Game(name=f"{os.environ['HEROKU_RELEASE_VERSION']} upcoming: {upcoming_track}"))
                    
                    with spotify.token_as(token):
                        logging.debug(f"rating {artist} - {name}")
                        try: 
                            result = Rating.create(user_id=uid, trackid=currently.item.id, rating=1)
                        except Exception as e:
                            logging.error(f"rating error: {e}")
                        
                        try:
                            logging.debug(f"queuing {upcoming_track} ({upcoming_tid})")
                            result = await spotify.playback_queue_add(track.uri)
                        except Exception as e:
                            logging.error(f"queuing error: {e}\n\n{result}")
                        
                        try:
                            insertedkey = PlayHistory.create(trackid=currently.item.id)
                        except Exception as e:
                            logging.error(f"playhistory exception: {e}")
                    sleep = (remaining_ms / 1000) +1
                else:
                    await bot.change_presence(activity=nextcord.Game(name=f"{os.environ['HEROKU_RELEASE_VERSION']} {nowplaying}"))
                    sleep = 30
            else :
                logging.debug(f"now playing {nowplaying}, {remaining_ms/1000}s remaining")
                sleep = (remaining_ms - 30000 ) / 1000

        logging.debug(f"watcher sleeping for {sleep} seconds")
        await asyncio.sleep(sleep)
    
    await bot.sendMessage("spotify watcher dying")


async def queue_manager():
    global users
    logging.info(f'starting queue manager')
    if len(users) == 0:
        users = ['212364275153371138']



    ratings = Rating.select(Rating.trackid, fn.SUM(Rating.rating)).group_by(Rating.trackid).where(Rating.user_id in [x for x in users])
    potentials = [x.trackid for x in ratings]
        # token = await getuser(user)
        # with spotify.token_as(token):
        #     history = history + [i.trackid for i in PlayHistory.select()]
        #     tops = tops + [item.id async for item in spotify.all_items(await spotify.current_user_top_tracks())]
        #     r = await spotify.recommendations(track_ids=[choice(tops)])
        #     recommendations = recommendations + [item.id for item in r.tracks]
        #     saveds = saveds + [item.track.id async for item in spotify.all_items(await spotify.saved_tracks())]
        #     potentials = [x for x in tops + saveds + recommendations if x not in history and x not in queue]
    logging.info(f"{len(potentials)} potential songs")
    
    while True:
        while len(queue) < 1:
                upcoming_tid =  choice(potentials)
                r = await spotify.recommendations(track_ids=[upcoming_tid]) 
                potentials.append(r.tracks[0].id)
                upcoming_track = await trackinfo(upcoming_tid)
                
                queue.append(upcoming_tid)

                logging.debug(f"upcoming track selected: {upcoming_track}")

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
    
