"""Print a new Web Push (VAPID) key pair, for the deployment's environment.

    python manage.py make_vapid_keys

Run it once, on your own machine, and paste the two lines into Dokploy
(Environment). Keep the private key out of the repository and out of chats.
Changing the pair later makes every browser subscribe again.
"""
from django.core.management.base import BaseCommand

from apps.store import webpush


class Command(BaseCommand):
    help = "Generate a Web Push (VAPID) key pair."

    def handle(self, *args, **opts):
        pub, priv = webpush.make_keys()
        self.stdout.write(f"WEBPUSH_PUBLIC_KEY={pub}")
        self.stdout.write(f"WEBPUSH_PRIVATE_KEY={priv}")
        self.stdout.write("WEBPUSH_SUBJECT=mailto:you@example.com")
