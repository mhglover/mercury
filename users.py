"""functions for user manipulations"""
import logging
import pickle
from models import User

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

async def getuser(cred, userid):
    """fetch user details
    
    returns: user object, spotify token
    """
    try:
        user = await User.get(spotifyid=userid)
    except Exception as e:
        logging.error("getuser exception fetching user\n%s", e)

    token = pickle.loads(user.token)
    if token.is_expiring:
        try:
            token = cred.refresh(token)
        except Exception as e:
            logging.error("getuser exception refreshing token\n%s", e)
        user.token = pickle.dumps(token)
        await user.save()

    return user, token


async def getactiveusers():
    """fetch details for the active users
    
    returns: list of Users
    """
    users = await User.exclude(status="inactive")
    return users
