import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Check your .env file."
    )


intents = discord.Intents.default()
intents.message_content = True


class GroupMateBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):
        await self.load_extension("commands.project")

        await self.tree.sync()

        print("Slash commands synced!")


bot = GroupMateBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print("GroupMate AI is online!")


@bot.tree.command(
    name="ping",
    description="Check whether GroupMate AI is online."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Pong! GroupMate AI is online. 🟢"
    )


bot.run(TOKEN)