from .jukebox import Jukebox

async def setup(bot):
    await bot.add_cog(Jukebox(bot))
