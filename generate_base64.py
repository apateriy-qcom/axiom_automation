import base64
import os

# Client Id & Secret (read from environment variables)
client_id = os.environ["AXIOM_CLIENT_ID"]
client_secret = os.environ["AXIOM_CLIENT_SECRET"]

# Concatenate client_id and client_secret
credentials = f"{client_id}:{client_secret}"

# Base64 encode the credentials
encoded_credentials = base64.b64encode(credentials.encode()).decode()

print(encoded_credentials)
