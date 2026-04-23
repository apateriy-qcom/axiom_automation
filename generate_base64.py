import base64

# Client Id & Secret
client_id = 'EnTJqIvoTeiJ4DOJzsnuEUbTWskGiHrF0ksEe0jxh6tMnPiD' #KEY
client_secret = 'MMsosPKqvsBDV79coEja8Hl5WvAAStgeGtKLqmHnCrUiA29z11gV3y93BavZZovl' #secret

# Concatenate client_id and client_secret
credentials = f"{client_id}:{client_secret}"

# Base64 encode the credentials
encoded_credentials = base64.b64encode(credentials.encode()).decode()

print(encoded_credentials)
