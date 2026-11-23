import routeros_api

connection = routeros_api.RouterOsApiPool(
    host="192.168.56.11",
    username="admin",
    password="123",
    port=8728,
    plaintext_login=True
)

api = connection.get_api()

print("BERHASIL TERHUBUNG KE MIKROTIK")

identity = api.get_resource("/system/identity")
print("Router:", identity.get()[0]["name"])

connection.disconnect()