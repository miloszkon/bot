import discord
from discord.ext import commands
from discord.ui import View, Button, Select, Modal, TextInput
import os
import asyncio
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)

SUPPORT_CATEGORY_ID = 1384251116493082675  # Kategoria ticketów
SUPPORT_ANNOUNCE_CHANNEL_ID = 1384272432654844085  # Kanał admin chat z pomysłami i zgłoszeniami
MANAGEMENT_ROLE_ID = 1319634655875432519  # Rola management

active_tickets = {}  # user_id -> {"type": str, "timestamp": datetime, "channel": channel, "closed": bool}
waiting_for_message = {}  # user_id -> "type"

# --- Komenda /ticket-info ---
@bot.tree.command(name="ticket-info", description="Informacje o systemie ticketów")
async def ticket_info(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📩 System Ticketów i Pomocy",
        description=(
            "Kliknij **HELP** poniżej, aby rozpocząć.\n\n"
            "Po kliknięciu bot wyśle Ci prywatną wiadomość z listą problemów."
        ),
        color=discord.Color.blue()
    )
    view = HelpButtonView()
    await interaction.response.send_message(embed=embed, view=view)

# --- Przycisk HELP ---
class HelpButtonView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(HelpButton())

class HelpButton(Button):
    def __init__(self):
        super().__init__(label="HELP", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_message("Sprawdź swoją prywatną wiadomość!", ephemeral=True)
            dm = await interaction.user.create_dm()
            await dm.send(
                "W czym możemy Ci pomóc? Wybierz problem z listy:",
                view=TicketSelectView(interaction.user)
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Nie mogę wysłać Ci wiadomości prywatnej. Ustaw, aby bot mógł pisać do Ciebie DM.", ephemeral=True
            )

# --- Wybór problemu ---
class TicketSelectView(View):
    def __init__(self, user):
        super().__init__(timeout=900)
        self.user = user
        self.add_item(TicketSelect())

class TicketSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Nie widzę kanałów", description="Problem z widocznością kanałów"),
            discord.SelectOption(label="Jak napisać rekrutację?", description="Pytanie o rekrutację"),
            discord.SelectOption(label="Mam problem z grą", description="Problem techniczny z grą"),
            discord.SelectOption(label="Mam pomysł na serwer", description="Chcę zgłosić pomysł"),
            discord.SelectOption(label="Połącz mnie z asystentem", description="Potrzebuję kontaktu z supportem")
        ]
        super().__init__(placeholder="Wybierz swój problem...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        choice = self.values[0]

        try:
            await interaction.message.delete()
        except:
            pass

        if choice == "Połącz mnie z asystentem":
            guild = None
            for g in bot.guilds:
                if g.get_member(user.id):
                    guild = g
                    break
            if not guild:
                await interaction.response.send_message("Nie znaleziono serwera.", ephemeral=True)
                return

            category = guild.get_channel(SUPPORT_CATEGORY_ID)
            if not category:
                await interaction.response.send_message("Nie znaleziono kategorii support.", ephemeral=True)
                return

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.get_member(user.id): discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.get_role(MANAGEMENT_ROLE_ID): discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            channel = await guild.create_text_channel(f'ticket-{user.name}', category=category, overwrites=overwrites)
            active_tickets[user.id] = {
                "type": "ticket",
                "timestamp": datetime.utcnow(),
                "channel": channel,
                "closed": False
            }
            await channel.send(f"{user.mention} otworzył ticket. Management może odpowiedzieć.", view=TicketActionView(channel))
            await interaction.response.send_message(f"Ticket został utworzony: {channel.mention}", ephemeral=True)
            bot.loop.create_task(ticket_inactivity_watchdog(user.id))
        else:
            waiting_for_message[user.id] = choice
            await interaction.response.send_message(
                f"Wybrałeś: **{choice}**.\nNapisz teraz wiadomość w tej prywatnej wiadomości.",
                ephemeral=True
            )

# --- Przyciski na kanale ticket ---
class TicketActionView(View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel
        self.add_item(ClimbButton(channel))
        self.add_item(RejectButton(channel))

class ClimbButton(Button):
    def __init__(self, channel):
        super().__init__(label="Climb", style=discord.ButtonStyle.success)
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        if MANAGEMENT_ROLE_ID not in [role.id for role in interaction.user.roles]:
            await interaction.response.send_message("Nie masz uprawnień.", ephemeral=True)
            return
        await self.channel.send(f"{interaction.user.mention} przejął ticket.")
        await interaction.response.defer()

class RejectButton(Button):
    def __init__(self, channel):
        super().__init__(label="Odrzuć", style=discord.ButtonStyle.danger)
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        if MANAGEMENT_ROLE_ID not in [role.id for role in interaction.user.roles]:
            await interaction.response.send_message("Nie masz uprawnień.", ephemeral=True)
            return

        ticket = next((t for t in active_tickets.values() if t["channel"].id == self.channel.id), None)
        if ticket:
            ticket["closed"] = True
            ticket["close_time"] = datetime.utcnow() + timedelta(minutes=5)
        await self.channel.send(f"Ticket zamknięty przez {interaction.user.mention}. Kanał zostanie usunięty za 5 minut.")
        await interaction.response.defer()
        bot.loop.create_task(delete_channel_after_delay(self.channel, 5*60))

async def delete_channel_after_delay(channel, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await channel.delete()
    except Exception as e:
        print(f"Nie udało się usunąć kanału: {e}")

# --- Watchdog ticketu na brak aktywności ---
async def ticket_inactivity_watchdog(user_id):
    while True:
        await asyncio.sleep(60)
        ticket = active_tickets.get(user_id)
        if not ticket or ticket["closed"]:
            return
        delta = datetime.utcnow() - ticket["timestamp"]
        if delta > timedelta(minutes=15):
            try:
                channel = ticket["channel"]
                await channel.send(f"Ticket zostanie zamknięty z powodu braku odpowiedzi użytkownika.")
                ticket["closed"] = True
                ticket["close_time"] = datetime.utcnow() + timedelta(minutes=5)
                bot.loop.create_task(delete_channel_after_delay(channel, 5*60))
            except Exception as e:
                print(f"Błąd przy zamykaniu ticketu: {e}")
            return

# --- Obsługa wiadomości DM od użytkowników ---
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel):
        if message.author.id in waiting_for_message:
            typ = waiting_for_message.pop(message.author.id)

            if message.author.id in active_tickets:
                active_tickets[message.author.id]["timestamp"] = datetime.utcnow()

            kanal_admin = bot.get_channel(SUPPORT_ANNOUNCE_CHANNEL_ID)
            if kanal_admin is None:
                await message.channel.send("Błąd: nie znaleziono kanału administracyjnego.")
                return

            embed = discord.Embed(
                title=f"💬 Nowa wiadomość: {typ}",
                description=message.content,
                color=discord.Color.orange(),
                timestamp=datetime.utcnow()
            )
            embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
            embed.set_footer(text=f"ID: {message.author.id}")

            view = AdminReplyView(message.author.id)

            await kanal_admin.send(embed=embed, view=view)
            await message.channel.send(embed=discord.Embed(
                description="✅ Twoja wiadomość została zapisana i przesłana do administracji.",
                color=discord.Color.green()
            ))
            return

    await bot.process_commands(message)

# --- Widok i przycisk odpowiedzi admina ---
class AdminReplyView(View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.add_item(AdminReplyButton(user_id))

class AdminReplyButton(Button):
    def __init__(self, user_id):
        super().__init__(label="Odpowiedz", style=discord.ButtonStyle.primary)
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        modal = ReplyModal(self.user_id)
        await interaction.response.send_modal(modal)

class ReplyModal(Modal):
    def __init__(self, user_id):
        super().__init__(title="Odpowiedź dla użytkownika")
        self.user_id = user_id
        self.response_input = TextInput(label="Twoja odpowiedź", style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.response_input)

    async def on_submit(self, interaction: discord.Interaction):
        user = bot.get_user(self.user_id)
        if not user:
            await interaction.response.send_message("Nie można znaleźć użytkownika.", ephemeral=True)
            return

        try:
            embed = discord.Embed(
                title="💬 Odpowiedź od administracji",
                description=self.response_input.value,
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text=f"Odpowiedź od: {interaction.user}")
            await user.send(embed=embed)
            await interaction.response.send_message("Odpowiedź została wysłana.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Nie można wysłać wiadomości użytkownikowi (DM zablokowane).", ephemeral=True)

# --- Flask keep-alive ---
app = Flask('')

@app.route('/')
def home():
    return "Bot działa!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- Uruchomienie ---
keep_alive()
TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)
