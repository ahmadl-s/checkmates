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


class TaskSelect(discord.ui.Select):
    def __init__(self, guild_id: int, project_tasks: list):
        self.guild_id = guild_id

        options = [
            discord.SelectOption(
                label=f"#{task['id']} {task['title']}"[:100],
                description=f"Status: {task['status']}"[:100],
                value=str(task["id"])
            )
            for task in project_tasks[:25]
        ]

        super().__init__(
            placeholder="Choose a task to update...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        task_id = int(self.values[0])
        project_tasks = tasks.get(self.guild_id, [])

        task = next(
            (t for t in project_tasks if t["id"] == task_id),
            None
        )

        if task is None:
            await interaction.response.send_message(
                "That task could not be found.",
                ephemeral=True
            )
            return

        view = TaskStatusView(self.guild_id, task_id)

        await interaction.response.edit_message(
            content=(
                f"### 📝 {task['title']}\n"
                f"**Current status:** {task['status']}\n\n"
                "Choose the new status below."
            ),
            view=view
        )


class TaskSelectView(discord.ui.View):
    def __init__(self, guild_id: int, project_tasks: list):
        super().__init__(timeout=180)
        self.add_item(TaskSelect(guild_id, project_tasks))


class UpdateDeadlineModal(discord.ui.Modal, title="Update Task Deadline"):
    deadline = discord.ui.TextInput(
        label="Deadline",
        placeholder="e.g. 25 Oct 2026, 11:59 PM",
        required=False,
        max_length=100
    )

    def __init__(self, guild_id: int, task_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.task_id = task_id

    async def on_submit(self, interaction: discord.Interaction):
        project = projects.get(self.guild_id)
        project_tasks = tasks.get(self.guild_id, [])

        if not project or interaction.user.id not in project["members"]:
            await interaction.response.send_message(
                "❌ You must be a project member to update deadlines.",
                ephemeral=True
            )
            return

        task = next(
            (t for t in project_tasks if t["id"] == self.task_id),
            None
        )

        if not task:
            await interaction.response.send_message(
                "❌ Task not found.",
                ephemeral=True
            )
            return

        new_deadline = self.deadline.value.strip()
        task["deadline"] = new_deadline or None

        embed = discord.Embed(
            title=f"📝 Task #{task['id']}: {task['title']}",
            description=task["description"],
            color=discord.Color.green()
            if task["status"] == "Completed"
            else discord.Color.blurple()
        )

        embed.add_field(
            name="📌 Status",
            value=task["status"],
            inline=True
        )

        assignee = interaction.guild.get_member(task["assignee"]) if task.get("assignee") else None
        embed.add_field(
            name="👤 Assigned To",
            value=assignee.mention if assignee else "Unassigned",
            inline=True
        )

        creator = interaction.guild.get_member(task["created_by"])
        embed.add_field(
            name="✍️ Created By",
            value=creator.mention if creator else "Unknown",
            inline=True
        )

        embed.add_field(
            name="📅 Deadline",
            value=task["deadline"] or "No deadline set",
            inline=True
        )

        embed.set_footer(text=f"{project['name']} • GroupMate AI")

        await interaction.response.send_message(
            "✅ Task deadline updated.",
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )


class TaskStatusView(discord.ui.View):
    def __init__(self, guild_id: int, task_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.task_id = task_id

    async def update_status(
        self,
        interaction: discord.Interaction,
        new_status: str
    ):
        project = projects.get(self.guild_id)

        if project is None or interaction.user.id not in project["members"]:
            await interaction.response.send_message(
                "Only project members can update tasks.",
                ephemeral=True
            )
            return

        project_tasks = tasks.get(self.guild_id, [])
        task = next(
            (t for t in project_tasks if t["id"] == self.task_id),
            None
        )

        if task is None:
            await interaction.response.send_message(
                "This task could not be found.",
                ephemeral=True
            )
            return

        old_status = task["status"]
        task["status"] = new_status

        status_emoji = {
            "To Do": "🟡",
            "In Progress": "🔵",
            "Completed": "🟢"
        }

        assignee = (
            f"<@{task['assignee']}>"
            if task.get("assignee")
            else "Unassigned"
        )

        creator = f"<@{task['created_by']}>"

        embed = discord.Embed(
            title=f"📝 Task #{task['id']}: {task['title']}",
            description=task["description"],
            color=discord.Color.green()
            if new_status == "Completed"
            else discord.Color.blurple()
        )

        embed.add_field(
            name="Status",
            value=f"{status_emoji[new_status]} {new_status}",
            inline=True
        )

        embed.add_field(
            name="Assigned To",
            value=assignee,
            inline=True
        )

        embed.add_field(
            name="Created By",
            value=creator,
            inline=True
        )

        embed.add_field(
            name="📅 Deadline",
            value=task.get("deadline") or "No deadline set",
            inline=True
        )

        embed.add_field(
            name="Previous Status",
            value=old_status,
            inline=False
        )

        embed.set_footer(
            text=f"{projects[self.guild_id]['name']} • GroupMate AI"
        )

        await interaction.response.edit_message(
            content="✅ **Task updated successfully!**",
            embed=embed,
            view=None,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @discord.ui.button(
        label="To Do",
        style=discord.ButtonStyle.secondary,
        emoji="🟡"
    )
    async def todo(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.update_status(interaction, "To Do")

    @discord.ui.button(
        label="In Progress",
        style=discord.ButtonStyle.primary,
        emoji="🔵"
    )
    async def in_progress(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.update_status(interaction, "In Progress")

    @discord.ui.button(
        label="Completed",
        style=discord.ButtonStyle.success,
        emoji="🟢"
    )
    async def completed(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.update_status(interaction, "Completed")

    @discord.ui.button(
        label="Update Deadline",
        style=discord.ButtonStyle.primary,
        emoji="📅",
        row=1
    )
    async def update_deadline(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(
            UpdateDeadlineModal(self.guild_id, self.task_id)
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
        assignee: discord.Member = None,
        deadline: str = None
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
            "status": "To Do",
            "deadline": deadline
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
            name="📅 Deadline",
            value=deadline if deadline else "No deadline set",
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

    @project_group.command(
        name="task-list",
        description="View all tasks for your project."
    )
    async def list_tasks(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Tasks can only be viewed inside a Discord server.",
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

        project_tasks = tasks.get(interaction.guild.id, [])

        if not project_tasks:
            await interaction.response.send_message(
                "There are no tasks yet. Use `/project task-create` to add one."
            )
            return

        embed = discord.Embed(
            title=f"📋 {project['name']} | Task Board",
            description=f"**Total tasks:** {len(project_tasks)}",
            color=discord.Color.blurple()
        )

        for task in project_tasks:
            assignee = (
                f"<@{task['assignee']}>"
                if task["assignee"]
                else "Unassigned"
            )

            status_emoji = {
                "To Do": "🟡",
                "In Progress": "🔵",
                "Completed": "🟢"
            }.get(task["status"], "⚪")

            embed.add_field(
                name=f"#{task['id']} · {task['title']}",
                value=(
                    f"**Status:** {status_emoji} {task['status']}\n"
                    f"**Assigned to:** {assignee}\n"
                    f"**Description:** {task['description']}\n"
                    f"**📅 Deadline:** {task.get('deadline') or 'No deadline set'}"
                ),
                inline=False
            )

        embed.set_footer(text="GroupMate AI • Project Task Board")

        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @project_group.command(
        name="task-update",
        description="Choose a task and update its status."
    )
    async def update_task(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Tasks can only be updated inside a Discord server.",
                ephemeral=True
            )
            return

        project = projects.get(interaction.guild.id)

        if project is None:
            await interaction.response.send_message(
                "This server doesn't have a project yet.",
                ephemeral=True
            )
            return

        if interaction.user.id not in project["members"]:
            await interaction.response.send_message(
                "Only project members can update tasks.",
                ephemeral=True
            )
            return

        project_tasks = tasks.get(interaction.guild.id, [])

        if not project_tasks:
            await interaction.response.send_message(
                "There are no tasks yet. Use `/project task-create` first.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "### 📋 Select a task to update",
            view=TaskSelectView(interaction.guild.id, project_tasks),
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(ProjectCommands(bot))