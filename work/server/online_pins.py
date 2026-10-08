"""
Certificates the launchers already know, so the first connection to these
servers is never taken on trust.

For any OTHER address the tunnel (tunnel.py) keeps the CA it is shown the first
time and refuses a different one afterwards. That first time is the one moment
somebody in the middle could lie; a server listed here does not have it.

    "ip:port": the server's certs/ca.crt, as PEM
"""

BUILTIN = {
    # The public Windstock server.
    "2.29.24.78:8888": """\
-----BEGIN CERTIFICATE-----
MIIDTzCCAjegAwIBAgIJAP4u6sBoHI9wMA0GCSqGSIb3DQEBCwUAMEUxCzAJBgNV
BAYTAlVTMRwwGgYDVQQKDBNQb0dPIFByaXZhdGUgU2VydmVyMRgwFgYDVQQDDA9Q
b0dPIFByaXZhdGUgQ0EwHhcNMjYxMDAxMDE1ODM2WhcNMzYwOTI4MDE1ODM2WjBF
MQswCQYDVQQGEwJVUzEcMBoGA1UECgwTUG9HTyBQcml2YXRlIFNlcnZlcjEYMBYG
A1UEAwwPUG9HTyBQcml2YXRlIENBMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIB
CgKCAQEAvsqgY69VuGkqvs4KBAXXAQgP3yzEpqUPRqXtFALsCv54JcHFKlt+idFp
hEhSAO6r5qQzPtPgsdNtabfg7XuJwkPq2ugEpjTwYYlc0vcIVHSzheVLH4/ZEMxM
vr+4TCpfUiUfBOTVE/xxmQx1x6PihwL1FLaYIJlSHaG50eclYO+V7uUuvxmuEx+g
SvAjhe+eqVSdXh5fDumEYMFmcUSu3x0qMnMlxN95Bvqdt9uBlM5zSW0kST66WUaj
uTY3CJg3chorO0IehwiWuNCTlOOR9zgL5rcdRBREODObIef0JwA220IEVxW1KNSO
nTnM2r3k/w7GayydTHFzZAKG8QNqmQIDAQABo0IwQDAPBgNVHRMBAf8EBTADAQH/
MA4GA1UdDwEB/wQEAwIBBjAdBgNVHQ4EFgQU/2h101g/XSabKPcpzeCozTjCC+ow
DQYJKoZIhvcNAQELBQADggEBAEBeavskxDSSnPdyWsKRE4D8B47FWFUmcz4Wi60d
IJ7EQ1gGD4RAdixEG1FIgM+IXzNRrMmw273WUdeK0+ZFCFoqu6+s+9gzDGzX131E
6sod3wqzvOH1Ho3mtpldRPwocauHQZ63Xni0nY7fo3tg+SfJ2Lez6ddkVhXhKqOd
eWA9N+9tzNnj/uwvj2Ug5T8s1uTUMC1Mc8DRFBhkt1Cl5SqHL5314TWsOxWZLzlF
k8UER1swz4MoJqISI5kpCXw3sITlw20fJAITsWPTB3ENueTtxKpmz7jG0jf0WTWV
kfpd7eFEJVe8Krvhvnv7DKCtHh6SzuA110WO/UL15empb9Q=
-----END CERTIFICATE-----
""",
}
