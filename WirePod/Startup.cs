using chewie.Services;

namespace WirePod
{
	/// <summary>Called at the end of RobotCommunicationService.Initialize (patched in by the Patcher).</summary>
	public static class Startup
	{
		public static void Attach(RobotCommunicationService service)
		{
			// Robot communication starts here, so make sure Android 12+ lets the app use Bluetooth.
			Permissions.RequestWhenReady();
		}
	}
}
