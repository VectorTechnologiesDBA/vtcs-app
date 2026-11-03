# Vector++ community server

A self-hosted server for the
[Vector++ community-servers app](https://github.com/Victor-Rebuild/vector-plus-plus-community-servers).
It runs next to **wire-pod** on the same machine (here a Raspberry Pi at `192.168.0.100`).

| Who | What | Done by |
|---|---|---|
| The app | Health check, create account, sign in, robot session certificate | **This server** (`vpp_server.py`) |
| Vector | Voice commands, jdocs, token server, weather, knowledge graph, SDK | **wire-pod** |

Session certificates come from wire-pod: it saves one when Vector authenticates, and this server hands it to the app.

Only Python 3's standard library is used, so there's nothing to install on the Pi.

## What the app calls

| Request | Answer |
|---|---|
| `GET /v1` | `{"status": "ok"}`. The app's server prompt checks this. |
| `POST /v1/create_user` `{"username", "password", "dob"}` | `200` with the user, `409` if the email is taken, `400` for a weak password |
| `POST /v1/sessions` `{"username", "password"}` | `200` with `{"session": {"session_token", "user_id", …}, "user": {…}}`, `403` for a wrong password |
| `GET /v1/session_cert/<name>_<esn>` | The PEM certificate from wire-pod's `/session-certs/<esn>` |

### Extra features

The app's **Extra features** (Weather and Knowledge settings, Talk, Drive with live camera, Quick actions, custom
eye colors) talk to wire-pod's web API through this server. It passes these on to wire-pod **only with a valid
session token** (`Authorization: Bearer <token from /v1/sessions>`, sent by the app automatically after sign-in):

| Path | Goes to wire-pod |
|---|---|
| `/api-sdk/*` | Robot control and settings (SDK) |
| `/api/*` | Weather and knowledge graph providers |
| `/cam-stream` | Vector's camera, streamed for Drive |

Anything else, or no or expired token: `401`. So people who can reach the port but have no account can't control
Vector. Turn off `allow_signup` once your accounts exist, otherwise anyone could make one.

The **SSH console** connects to Vector directly (not through this server), with the SSH key built into the app.

Accounts are saved in `data/users.json`, with passwords hashed using scrypt. Sessions are saved in `data/sessions.json`.

## Set up on the Pi

1. **Copy the folder over.** Run this on your PC:

   ```bash
   scp -r "C:\re\work\v++ community servers\community-server" hayes@192.168.0.100:/home/hayes/vpp-server
   ```

2. **Create the config.** On the Pi:

   ```bash
   cd ~/vpp-server && cp config.example.json config.json && nano config.json
   ```

   - **`cert_file` / `key_file`:** where your `server.vectortech.dev` certificate and key are. If they're in
     `/etc/letsencrypt`, the key can only be read by root. Either copy both files somewhere `hayes` can read,
     or run the service as root.
   - **`port`:** keep **8443**. wire-pod already uses 443 on this machine for Vector.
   - **`wirepod_url`:** `http://127.0.0.1:8080` when wire-pod runs on the same Pi.
   - **`allow_signup`:** set to `false` after you've created your accounts.

3. **Try it.**

   ```bash
   python3 ~/vpp-server/vpp_server.py
   ```

   Then visit `https://server.vectortech.dev:8443/v1` from another device. It should show `{"status": "ok", …}`.

4. **Start it automatically.**

   ```bash
   sudo cp ~/vpp-server/vpp-server.service /etc/systemd/system/ && sudo systemctl enable --now vpp-server
   ```

   To see its log: `journalctl -u vpp-server -f`

## Make the name work

The phone has to reach `server.vectortech.dev` at the Pi:

- **At home:** point `server.vectortech.dev` at `192.168.0.100` in your router's DNS, or set it in DNS if it
  should only ever be used at home.
- **Away from home:** forward port 8443 on your router to the Pi, and point the name at your public IP.

The certificate must cover `server.vectortech.dev` and must not have expired. Android rejects anything else.

## In the app

1. In the server prompt, enter `server.vectortech.dev:8443` (include the port; the app assumes 443 otherwise).
2. Tap **Create account** with an email address and a password of 8–32 characters with an uppercase letter,
   a lowercase letter and a digit. Then sign in.
3. Set up Vector as usual. He needs to be connected to wire-pod (wire-pod's "Bot Setup" page), so that wire-pod
   has his session certificate.
