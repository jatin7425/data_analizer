from django.test import TestCase
from django.urls import reverse


class AuthenticationFormVisibilityTests(TestCase):
    def test_login_inputs_use_visible_text_colors(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertContains(response, "color: #f8fafc !important;")
        self.assertContains(response, "background-color: #0f172a;")
        self.assertContains(response, "-webkit-text-fill-color: #f8fafc;")

    def test_signup_inputs_use_visible_text_colors(self):
        response = self.client.get(reverse("accounts:signup"))

        self.assertContains(response, "color: #f8fafc !important;")
        self.assertContains(response, "background-color: #0f172a;")
        self.assertContains(response, "-webkit-text-fill-color: #f8fafc;")
