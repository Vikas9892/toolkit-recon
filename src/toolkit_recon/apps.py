"""The 100 apps under study.

`official_domains` is load-bearing, not decoration. The ranker uses it to
prefer vendor documentation over blogspam, and `confidence.py` uses it to
decide whether we genuinely reached official docs — which caps a row at
"low" when we did not. Domains are listed apex-first; subdomain matching is
handled in ranking.py.

The list is deliberately spread across categories and across the difficulty
range: obvious self-serve REST APIs (Stripe, GitHub), admin-gated enterprise
suites (Workday, NetSuite), partner-gated platforms (WhatsApp Business), and
products where the API story is narrower than the marketing suggests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppSpec:
    name: str
    slug: str
    category: str
    official_domains: tuple[str, ...]


def _a(name: str, slug: str, category: str, *domains: str) -> AppSpec:
    return AppSpec(name=name, slug=slug, category=category, official_domains=domains)


APPS: list[AppSpec] = [
    # ---------------- CRM & Sales ----------------
    _a("Salesforce", "salesforce", "CRM & Sales", "salesforce.com", "force.com"),
    _a("HubSpot", "hubspot", "CRM & Sales", "hubspot.com"),
    _a("Pipedrive", "pipedrive", "CRM & Sales", "pipedrive.com"),
    _a("Zoho CRM", "zoho_crm", "CRM & Sales", "zoho.com"),
    _a("Close", "close", "CRM & Sales", "close.com"),
    _a("Copper", "copper", "CRM & Sales", "copper.com"),
    _a("Freshsales", "freshsales", "CRM & Sales", "freshworks.com"),
    _a("Outreach", "outreach", "CRM & Sales", "outreach.io"),
    _a("Salesloft", "salesloft", "CRM & Sales", "salesloft.com"),
    _a("Apollo.io", "apollo_io", "CRM & Sales", "apollo.io"),
    # ---------------- Communication ----------------
    _a("Slack", "slack", "Communication", "slack.com", "slack.dev"),
    _a("Microsoft Teams", "microsoft_teams", "Communication", "microsoft.com"),
    _a("Discord", "discord", "Communication", "discord.com"),
    _a("Zoom", "zoom", "Communication", "zoom.us", "zoom.com"),
    _a("Twilio", "twilio", "Communication", "twilio.com"),
    _a("WhatsApp Business Platform", "whatsapp_business", "Communication",
       "whatsapp.com", "facebook.com", "meta.com"),
    _a("Telegram", "telegram", "Communication", "telegram.org"),
    _a("Intercom", "intercom", "Communication", "intercom.com"),
    _a("Front", "front", "Communication", "frontapp.com", "front.com"),
    _a("RingCentral", "ringcentral", "Communication", "ringcentral.com"),
    # ---------------- Project Management ----------------
    _a("Jira", "jira", "Project Management", "atlassian.com"),
    _a("Asana", "asana", "Project Management", "asana.com"),
    _a("Trello", "trello", "Project Management", "trello.com", "atlassian.com"),
    _a("Monday.com", "monday", "Project Management", "monday.com"),
    _a("Linear", "linear", "Project Management", "linear.app"),
    _a("ClickUp", "clickup", "Project Management", "clickup.com"),
    _a("Notion", "notion", "Project Management", "notion.com", "notion.so"),
    _a("Basecamp", "basecamp", "Project Management", "basecamp.com"),
    _a("Wrike", "wrike", "Project Management", "wrike.com"),
    _a("Smartsheet", "smartsheet", "Project Management", "smartsheet.com"),
    # ---------------- Developer Tools ----------------
    _a("GitHub", "github", "Developer Tools", "github.com"),
    _a("GitLab", "gitlab", "Developer Tools", "gitlab.com"),
    _a("Bitbucket", "bitbucket", "Developer Tools", "bitbucket.org", "atlassian.com"),
    _a("Sentry", "sentry", "Developer Tools", "sentry.io"),
    _a("Datadog", "datadog", "Developer Tools", "datadoghq.com"),
    _a("PagerDuty", "pagerduty", "Developer Tools", "pagerduty.com"),
    _a("CircleCI", "circleci", "Developer Tools", "circleci.com"),
    _a("Vercel", "vercel", "Developer Tools", "vercel.com"),
    _a("Netlify", "netlify", "Developer Tools", "netlify.com"),
    _a("Docker Hub", "docker_hub", "Developer Tools", "docker.com"),
    _a("New Relic", "new_relic", "Developer Tools", "newrelic.com"),
    _a("Grafana Cloud", "grafana_cloud", "Developer Tools", "grafana.com"),
    # ---------------- Storage & Documents ----------------
    _a("Google Drive", "google_drive", "Storage & Documents", "google.com"),
    _a("Dropbox", "dropbox", "Storage & Documents", "dropbox.com"),
    _a("Box", "box", "Storage & Documents", "box.com"),
    _a("Microsoft OneDrive", "onedrive", "Storage & Documents", "microsoft.com"),
    _a("Google Sheets", "google_sheets", "Storage & Documents", "google.com"),
    _a("Airtable", "airtable", "Storage & Documents", "airtable.com"),
    _a("Confluence", "confluence", "Storage & Documents", "atlassian.com"),
    _a("Coda", "coda", "Storage & Documents", "coda.io"),
    # ---------------- Marketing ----------------
    _a("Mailchimp", "mailchimp", "Marketing", "mailchimp.com"),
    _a("Klaviyo", "klaviyo", "Marketing", "klaviyo.com"),
    _a("SendGrid", "sendgrid", "Marketing", "sendgrid.com", "twilio.com"),
    _a("Adobe Marketo Engage", "marketo", "Marketing", "marketo.com", "adobe.com"),
    _a("Braze", "braze", "Marketing", "braze.com"),
    _a("Customer.io", "customer_io", "Marketing", "customer.io"),
    _a("ActiveCampaign", "activecampaign", "Marketing", "activecampaign.com"),
    _a("Brevo", "brevo", "Marketing", "brevo.com"),
    _a("Iterable", "iterable", "Marketing", "iterable.com"),
    _a("Segment", "segment", "Marketing", "segment.com", "twilio.com"),
    # ---------------- Customer Support ----------------
    _a("Zendesk", "zendesk", "Customer Support", "zendesk.com"),
    _a("Freshdesk", "freshdesk", "Customer Support", "freshdesk.com", "freshworks.com"),
    _a("Help Scout", "help_scout", "Customer Support", "helpscout.com"),
    _a("Gorgias", "gorgias", "Customer Support", "gorgias.com"),
    _a("Zoho Desk", "zoho_desk", "Customer Support", "zoho.com"),
    _a("Kustomer", "kustomer", "Customer Support", "kustomer.com"),
    _a("Crisp", "crisp", "Customer Support", "crisp.chat"),
    # ---------------- HR & Recruiting ----------------
    _a("Workday", "workday", "HR & Recruiting", "workday.com"),
    _a("BambooHR", "bamboohr", "HR & Recruiting", "bamboohr.com"),
    _a("Gusto", "gusto", "HR & Recruiting", "gusto.com"),
    _a("Rippling", "rippling", "HR & Recruiting", "rippling.com"),
    _a("Greenhouse", "greenhouse", "HR & Recruiting", "greenhouse.io"),
    _a("Lever", "lever", "HR & Recruiting", "lever.co"),
    _a("Deel", "deel", "HR & Recruiting", "deel.com"),
    # ---------------- Finance & Accounting ----------------
    _a("Stripe", "stripe", "Finance & Accounting", "stripe.com"),
    _a("QuickBooks Online", "quickbooks", "Finance & Accounting", "intuit.com", "quickbooks.com"),
    _a("Xero", "xero", "Finance & Accounting", "xero.com"),
    _a("Oracle NetSuite", "netsuite", "Finance & Accounting", "netsuite.com", "oracle.com"),
    _a("Plaid", "plaid", "Finance & Accounting", "plaid.com"),
    _a("Brex", "brex", "Finance & Accounting", "brex.com"),
    _a("Ramp", "ramp", "Finance & Accounting", "ramp.com"),
    _a("BILL", "bill_com", "Finance & Accounting", "bill.com"),
    _a("Expensify", "expensify", "Finance & Accounting", "expensify.com"),
    # ---------------- Analytics & Data ----------------
    _a("Snowflake", "snowflake", "Analytics & Data", "snowflake.com"),
    _a("Databricks", "databricks", "Analytics & Data", "databricks.com"),
    _a("Looker", "looker", "Analytics & Data", "looker.com", "google.com"),
    _a("Tableau", "tableau", "Analytics & Data", "tableau.com", "salesforce.com"),
    _a("Amplitude", "amplitude", "Analytics & Data", "amplitude.com"),
    _a("Mixpanel", "mixpanel", "Analytics & Data", "mixpanel.com"),
    _a("Microsoft Power BI", "power_bi", "Analytics & Data", "microsoft.com"),
    _a("Google Analytics 4", "google_analytics", "Analytics & Data", "google.com"),
    # ---------------- E-commerce ----------------
    _a("Shopify", "shopify", "E-commerce", "shopify.com", "shopify.dev"),
    _a("WooCommerce", "woocommerce", "E-commerce", "woocommerce.com"),
    _a("BigCommerce", "bigcommerce", "E-commerce", "bigcommerce.com"),
    _a("Squarespace", "squarespace", "E-commerce", "squarespace.com"),
    _a("Etsy", "etsy", "E-commerce", "etsy.com"),
    # ---------------- Scheduling, Design & Signature ----------------
    _a("Calendly", "calendly", "Scheduling", "calendly.com"),
    _a("Figma", "figma", "Design", "figma.com"),
    _a("DocuSign", "docusign", "E-signature", "docusign.com"),
    _a("Canva", "canva", "Design", "canva.com", "canva.dev"),
]

BY_SLUG = {a.slug: a for a in APPS}

assert len(APPS) == 100, f"expected 100 apps, found {len(APPS)}"
assert len(BY_SLUG) == 100, "duplicate slug in APPS"
