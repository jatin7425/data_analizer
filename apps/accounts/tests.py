from django.test import TestCase
from django.urls import reverse


class AuthenticationPageTests(TestCase):
    def test_login_page_renders_labelled_fields_with_shared_styles(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertContains(response, "analyzer/app.css")
        self.assertContains(response, '<label for="id_username">')
        self.assertContains(response, '<label for="id_password">')
        self.assertContains(response, reverse("accounts:signup"))

    def test_signup_page_renders_labelled_fields_with_shared_styles(self):
        response = self.client.get(reverse("accounts:signup"))

        self.assertContains(response, "analyzer/app.css")
        self.assertContains(response, '<label for="id_username">')
        self.assertContains(response, '<label for="id_email">')
        self.assertContains(response, '<label for="id_password1">')
        self.assertContains(response, reverse("accounts:login"))
