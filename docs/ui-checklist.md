# UI checklist

Run against the example project. Check each page at 1280×720 and 390×844, in light and dark, for
readable text, no sideways scrolling, and usable buttons.

```bash
cd example
python manage.py migrate && python manage.py seed_demo && python manage.py runserver
```

All passwords are `demo-only`.

| Sign in as | Go to | Expect |
|---|---|---|
| Nobody | `/t/acme/` | The sign-in page, then back to `/t/acme/` |
| owner | `/tenants/` | The switcher, listing Acme only |
| owner | `/t/acme/` | Acme's address book, "owner", add, edit and delete links, and invite and request links |
| viewer | `/t/acme/` | Acme's address book, "viewer", no add or edit links, and no invite link |
| member | `/t/acme/members/invite/` | 403 |
| owner | `/t/acme/members/invite/` | The form, with owner offered as a role |
| admin | `/t/acme/members/invite/` | The form, without owner. Submitting shows a link |
| outsider | `/t/acme/` | The join page, and "Ask to join" confirms it was sent |
| owner | `/t/acme/members/requests/` | outsider's request. Approve removes it, and outsider can enter |
| outsider | `/t/nope/` | 404 |
| former | `/t/acme/` | "Your access has been removed" |
| owner | `/t/closed/` | "This workspace is inactive" |
| root | `/t/globex/` | Globex's address book, "working as staff", and a row in Staff access logs |
| Invited person | The invitation link | The accept page, and accepting opens the switcher |
| Anyone else | The same link | "This invitation can't be used" |
| owner | `/t/acme/contacts/add/` | The form. Saving returns to the list with the new contact |
| owner | Search for "Globex" on `/t/acme/` | "No contacts found" |
| Nobody | `/signup/` | The form. Submitting shows "Check your email", and the link prints in the server's terminal. Email encoding wraps it: join the two lines and remove the `=` |
| Nobody | The activation link | "Activate", and the button opens the switcher with the new workspace |
| Nobody | The same link again | "This link can't be used" |
