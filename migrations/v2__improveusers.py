import os
# import psycopg2

from playhouse.db_url import connect
from playhouse.migrate import *

pgdb = connect(os.environ['DATABASE_URL'], autorollback=True)
migrator = PostgresqlMigrator(pgdb)

tablename = 'user'

migrate(
    migrator.add_column(tablename, 'email', TextField(default='')),
    migrator.add_column(tablename, 'display_name', TextField(default='')),
    migrator.add_column(tablename, 'href', TextField(default='')),
)

