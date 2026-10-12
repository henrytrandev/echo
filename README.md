# Echo

Meet people whose music taste can put you onto new artists.

![Match list showing shared artists and new artists each match could put you onto](docs/screenshot.png)

Most "taste match" apps find your musical twin. But a twin can't show you anything new. Echo looks for people who share enough of your core taste that you'll trust their picks, **and** who love artists you haven't found yet that are likely to suit you. Each match shows what you have in common, what they could put you onto, and what you could put them onto. Names stay hidden until both people choose to connect.

## Every match explains itself

- **Because you like…** Each suggested artist names the artist of yours it is most often heard alongside, using the same leave-one-out co-listening counts as the score, weighted towards niche links.
- **Why this match?** Each card opens to show its overlap, its discovery in both directions, and the score worked out: `√(overlap × smaller discovery)`.
- **Taste fingerprint.** Every listener gets a barcode of up to 50 bars, one per artist, taller for the ones they play most. It carries no names, so it can appear on anonymous matches.
- **Share card.** One click draws your fingerprint and top five artists into a 1080×1350 image you can download and post.

## Listening, feedback and saving songs

Press any artist under **They could put you onto** and a song by them plays in the player at the bottom of the page, with a record that spins while the music plays. Logged-in users can add the song to an **Echo finds** playlist that the app creates in their Spotify account.

- Songs play through Spotify's own embedded player. Visitors usually hear a preview clip; people logged in to Spotify in the same browser may hear the full song.
- Spotify no longer offers an "artist top tracks" endpoint to development-mode apps, so Echo searches for a song credited to the artist and caches the answer in the `artist_tracks` table. Demo artists, which arrive without Spotify IDs, get theirs filled in on the first lookup.
- Saving needs the `playlist-modify-private` and `playlist-modify-public` scopes. Anyone who logged in before this feature existed sees an **Allow playlist saving** button, which sends them through Spotify login again to approve it.
- If someone deletes the playlist in Spotify, the next save creates a new one.

The player has its own controls: play/pause, replay from the start, next, and a progress bar you can drag or click to jump. All the suggestions on the page form one queue, so **next** moves to the next artist anyone could put you onto, and when a song ends the next one starts, like a radio.

Each song can get feedback:

- **Love it** marks the artist with a heart everywhere it appears.
- **Not for me** hides the artist from your suggestions on every card, skips to the next song, and offers an undo. The server keeps it hidden on later visits too.
- Feedback is stored in the `song_feedback` table (`PUT /api/feedback/{artist_id}` with `{"verdict": "like" | "dislike" | null}`), one row per person and artist. Besides hiding dislikes, these rows are the labels for the offline evaluation on the roadmap. Demo visitors can try both buttons, but their feedback stays in the page and is never saved.

## How matching works

Every listener's Spotify top artists (short, medium and long term) become one affinity score per artist, weighted by rank and time range. Then every pair of listeners gets two scores:

- **Overlap** is the cosine similarity of IDF-weighted profiles. Sharing a niche artist counts far more than sharing a superstar everyone listens to.
- **Discovery** measures how much someone could put you onto. For each artist they love that you don't know, it multiplies how much that artist matters to them by how well it fits your taste. Fit comes from co-listening across all users ("people who like X also like Y"), computed with the candidate left out so their own profile can't vouch for itself.

The final score is `overlap^α × discovery^(1−α)`, using the smaller of the two discovery directions so both people gain something. An identical twin scores zero.

Retrieval is two-stage: overlap for every candidate is a single sparse matrix-vector product, and the more expensive discovery score is computed only for the top 200 by overlap. At this app's scale (dozens to thousands of users) exact scoring is fast and needs no approximate index.

The code is in [`echo/matching/engine.py`](echo/matching/engine.py) and has no database or web dependencies, so it is easy to test and reuse.

**Known limitation:** "fits your taste" is learned from co-listening across everyone in the database. With only a handful of real users there is little of that evidence, so discovery scores are mostly zero and matches fall back to ranking by overlap. Demo listeners help (they always count toward these statistics, even when hidden as matches), but only for the artists they include. Learning artist similarity from public ListenBrainz data is the planned fix; see the roadmap.

## Setup

### 1. Create a Spotify app

1. Go to the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and create an app. The app owner's account needs Spotify Premium.
2. Under **Redirect URIs**, add `http://127.0.0.1:8000/callback`. Use `127.0.0.1`, not `localhost`; Spotify only allows plain `http` for loopback IP addresses.
3. Under **User Management**, add the Spotify email of each friend who will try the app. A development-mode app allows **5 users** in total.
4. Copy the client ID and client secret.

### 2. Install and configure

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m echo keys        # paste the two values into .env
```

Then fill in `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env`.

### 3. Run

```bash
python -m echo seed-demo   # optional: 150 sample listeners so you see matches right away
uvicorn echo.web.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and log in with Spotify. Interactive API docs are at http://127.0.0.1:8000/docs.

Demo listeners are labelled as sample data and can't be contacted. Set `SHOW_DEMO_USERS=false` before inviting real people.

### Try the demo

Spotify limits development-mode apps to 5 users, so anyone else who opens the app would hit a login error. The **Try the demo** button (`/demo`) lets any visitor look around as a random demo listener instead, with no login. Demo visitors only ever see other demo listeners, never real users, and can't change settings or send connection requests.

### Deploying

[DEPLOY.md](DEPLOY.md) walks through running it on Render with a Neon Postgres database, on a subdomain of an existing website (Squarespace in that guide).

### Tests

```bash
pytest
```

The suite covers the matching math, the Spotify client (against a fake Spotify), the database layer, and the full login-to-connection flow through the API.

### Keeping profiles fresh

`python -m echo refresh` re-imports top artists for anyone not refreshed in the last 24 hours. Run it on a schedule (cron, or a scheduled GitHub Action). It fetches from Spotify concurrently and writes each user in their own transaction, so one failure never affects anyone else.

### Postgres

SQLite is the default. To use Postgres:

```bash
docker compose up -d db
pip install -e ".[postgres]"
# in .env: DATABASE_URL=postgresql+psycopg://echo:echo@127.0.0.1:5432/echo
```

## Project layout

```
echo/
  matching/       profiles and the match engine (pure numpy/scipy)
  spotify/        OAuth helpers, retrying async client, response parsing
  web/            FastAPI app and the single-page frontend
  sources.py      the one type every data source converts into
  repository.py   all database reads and writes, including artist entity resolution
  ingest.py       login import and the scheduled refresh job
  models.py       SQLAlchemy schema
  demo.py         synthetic listeners for local testing
tests/
```

## Design decisions

- **Artists, not tracks.** Two people can love the same artist without sharing a single song; track-level overlap is too sparse to be useful.
- **Source-agnostic ingestion.** Every source converts into `SourceArtist`, and artists are resolved by Spotify ID, then MusicBrainz ID, then normalized name. Adding Last.fm or a Spotify data-export upload means writing one adapter, with no changes to matching.
- **Snapshot ingestion.** Each import replaces a user's rows for the time ranges it fetched, so re-running is always safe.
- **Tokens encrypted at rest** with Fernet; sessions and the OAuth `state` check use signed cookies.
- **Privacy by default.** Matches show an anonymous alias until both people connect, and anyone can turn off "Let others find me".
- **A public demo that can't leak real data.** Demo sessions are read-only and only ever see generated listeners.
- **Minimal scope.** The app requests `user-top-read` plus the two playlist scopes, and only ever writes to the one playlist it created.

## Spotify API limits (as of October 2026)

Spotify's February 2026 changes restrict development-mode apps to 5 authorized users and require the owner to have Premium. Endpoints this app depends on (`/me`, `/me/top/artists`) are still available. Because of the user cap, the planned next data sources are Last.fm and Spotify data-export uploads, neither of which has that limit.

## Roadmap

1. **Offline evaluation.** Hold out the artists each listener picked up later, and measure whether their matches' suggestions predicted them (recall@k). Compare similarity-only matching against discovery-weighted matching. "Love it" and "Not for me" feedback gives a second, explicit signal: the share of suggestions people love versus reject, per matching variant.
2. **More sources.** Last.fm login and Spotify data-export upload, both mapped through MusicBrainz IDs.
3. **Learned artist embeddings.** Train implicit ALS on ListenBrainz listening data to replace co-occurrence similarity, so the app can judge fit for artists no current user has heard.
4. **Song sharing.** Send a connected friend a song with a note. Track whether the songs people save from matches stick.
5. **Scale.** Cache the engine between requests, move to Alembic migrations, and benchmark an HNSW index (pgvector) against exact scoring at 100k+ synthetic profiles.
