import discord
from discord import app_commands
from discord.ext import commands


# Temporary storage. We will replace this with the backend API later.
projects = {}
tasks = {}

class CreateProjectModal(discord.ui.Modal):
    def __init__(self, guild_id: int, creator_id: int):
        super().__init__(title="Create Your Project")

        self.guild_id = guild_id
        self.creator_id = creator_id

        self.project_name = discord.ui.TextInput(
            label="Project Name",
            placeholder="e.g. checkmates",
            max_length=100
        )

        self.project_description = discord.ui.TextInput(
            label="Project Description",
            placeholder="What is your team building?",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000
        )

        self.add_item(self.project_name)
        self.add_item(self.project_description)

    async def on_submit(self, interaction: discord.Interaction):
        if self.guild_id in projects:
            await interaction.response.send_message(
                "This server already has a project!",
                ephemeral=True
            )
            return

        view = ProjectSetupView(
            guild_id=self.guild_id,
            creator_id=self.creator_id,
            name=self.project_name.value.strip(),
            description=(
                self.project_description.value.strip()
                or "No description provided."
            )
        )

        await interaction.response.send_message(
            "### 👥 Select Your Project Members\n"
            "Choose your teammates below, then click **Create Project**. "
            "You can skip this step if you're working alone.",
            view=view,
            ephemeral=True
        )


class ProjectSetupView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        creator_id: int,
        name: str,
        description: str
    ):
        super().__init__(timeout=300)

        self.guild_id = guild_id
        self.creator_id = creator_id
        self.name = name
        self.description = description
        self.selected_members = []

        self.member_select = discord.ui.UserSelect(
            placeholder="Search for and select teammates",
            min_values=0,
            max_values=24,
            row=0
        )

        self.member_select.callback = self.select_members
        self.add_item(self.member_select)

    async def select_members(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the project creator can configure this project.",
                ephemeral=True
            )
            return

        self.selected_members = list(self.member_select.values)

        await interaction.response.defer()

    @discord.ui.button(
        label="Create Project",
        style=discord.ButtonStyle.success,
        emoji="🚀",
        row=1
    )
    async def create_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the project creator can create this project.",
                ephemeral=True
            )
            return

        if self.guild_id in projects:
            await interaction.response.edit_message(
                content="This server already has a project!",
                view=None
            )
            return

        member_ids = {self.creator_id}

        for member in self.selected_members:
            if not member.bot:
                member_ids.add(member.id)

        project = {
            "name": self.name,
            "description": self.description,
            "guild_id": self.guild_id,
            "created_by": self.creator_id,
            "members": list(member_ids)
        }

        projects[self.guild_id] = project

        embed = discord.Embed(
            title=f"{self.name}",
            description="Project created successfully!",
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Description",
            value=self.description,
            inline=False
        )

        embed.add_field(
            name="Project Manager",
            value=f"<@{self.creator_id}>",
            inline=True
        )

        embed.add_field(
            name="Members",
            value=str(len(member_ids)),
            inline=True
        )

        embed.add_field(
            name="Team",
            value="\n".join(
                f"<@{member_id}>" for member_id in member_ids
            ),
            inline=False
        )

        embed.set_footer(
            text="checkmates • Your team's AI project manager"
        )

        await interaction.channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )

        await interaction.response.edit_message(
            content="✅ Project created! The announcement is now public.",
            view=None
        )

class ProjectCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    project_group = app_commands.Group(
        name="project",
        description="Manage your team's project."
    )

    @project_group.command(
        name="create",
        description="Create your team's project."
    )
    async def create_project(
        self,
        interaction: discord.Interaction
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Projects can only be created inside a Discord server.",
                ephemeral=True
            )
            return

        if interaction.guild.id in projects:
            await interaction.response.send_message(
                "This server already has a project! "
                "Use `/project view` to see its details.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            CreateProjectModal(
                guild_id=interaction.guild.id,
                creator_id=interaction.user.id
            )
        )

    @project_group.command(
        name="view",
        description="View your team's project details."
    )
    async def view_project(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Projects can only be viewed inside a Discord server."
            )
            return

        project = projects.get(interaction.guild.id)

        if project is None:
            await interaction.response.send_message(
                "This server doesn't have a project yet. "
                "Use `/project create` to get started."
            )
            return

        try:
            creator = await interaction.guild.fetch_member(project["created_by"])
        except discord.NotFound:
            creator = None
        except discord.HTTPException:
            creator = None

        embed = discord.Embed(
            title="📋 Project Overview",
            description=f"**{project['name']}**",
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Description",
            value=project["description"],
            inline=False
        )

        embed.add_field(
            name="Project Manager",
            value=creator.mention if creator else "Unknown",
            inline=True
        )

        embed.add_field(
            name="Members",
            value=str(len(project["members"])),
            inline=True
        )

        embed.add_field(
            name="👥 Team Members",
            value="\n".join(
                f"<@{member_id}>"
                for member_id in project["members"]
            ),
            inline=False
        )

        embed.set_footer(
            text="checkmates • Your team's AI project manager"
        )

        await interaction.response.send_message(
            embed=embed
        )

    @project_group.command(
        name="task-create",
        description="Create a task for your project."
    )
    @app_commands.describe(
        title="What needs to be done?",
        description="Additional details about the task",
        assignee="The teammate responsible for this task"
    )
    async def create_task(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str = "No description provided.",
        assignee: discord.Member = None
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Tasks can only be created inside a Discord server.",
                ephemeral=True
            )
            return

        project = projects.get(interaction.guild.id)

        if project is None:
            await interaction.response.send_message(
                "This server doesn't have a project yet. "
                "Use `/project create` first.",
                ephemeral=True
            )
            return

        if interaction.user.id not in project["members"]:
            await interaction.response.send_message(
                "Only project members can create tasks.",
                ephemeral=True
            )
            return

        if assignee is not None and assignee.id not in project["members"]:
            await interaction.response.send_message(
                "The assignee must be a member of this project.",
                ephemeral=True
            )
            return

        task_id = len(tasks.get(interaction.guild.id, [])) + 1

        task = {
            "id": task_id,
            "title": title,
            "description": description,
            "assignee": assignee.id if assignee else None,
            "created_by": interaction.user.id,
            "status": "To Do"
        }

        tasks.setdefault(interaction.guild.id, []).append(task)

        embed = discord.Embed(
            title=f"📝 Task #{task_id}: {title}",
            description=description,
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Status",
            value="🟡 To Do",
            inline=True
        )

        embed.add_field(
            name="Assigned To",
            value=assignee.mention if assignee else "Unassigned",
            inline=True
        )

        embed.add_field(
            name="Created By",
            value=interaction.user.mention,
            inline=True
        )

        embed.set_footer(text=f"{project['name']} • GroupMate AI")

        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ProjectCommands(bot))