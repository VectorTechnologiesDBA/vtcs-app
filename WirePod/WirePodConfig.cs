using System;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Threading.Tasks;
using Android.App;
using Android.Content;
using chewie.Services;
using core.Services;
using Xamarin.Android.Net;

namespace WirePod
{
	/// <summary>
	/// Where the wire-pod server lives, and the wire-pod URLs that replace Anki cloud URLs.
	/// The address is entered by the user at sign-in and kept in the "wirepod" shared preferences.
	/// </summary>
	public static class WirePodConfig
	{
		public static string Server
		{
			get
			{
				if (WirePodConfig.kFixedServer)
				{
					return WirePodConfig.kDefaultHost + ":" + WirePodConfig.kFixedPort;
				}
				string text = WirePodConfig.Prefs.GetString(WirePodConfig.kServerKey, null);
				if (string.IsNullOrEmpty(text))
				{
					return WirePodConfig.kDefaultHost + ":" + WirePodConfig.kDefaultWebPort;
				}
				return text;
			}
		}

		public static bool HasServer
		{
			get
			{
				return WirePodConfig.kFixedServer || !string.IsNullOrEmpty(WirePodConfig.Prefs.GetString(WirePodConfig.kServerKey, null));
			}
		}

		/// <summary>wire-pod's web server, e.g. http://192.168.1.20:8080</summary>
		public static string WebBaseUrl
		{
			get
			{
				return "https://" + WirePodConfig.Server;
			}
		}

		/// <summary>The "Extra features" switch (set by the Java ExtrasActivity, same preferences).</summary>
		public static bool ExtrasEnabled
		{
			get
			{
				return WirePodConfig.Prefs.GetString("extras_enabled", "0") == "1";
			}
		}

		/// <summary>One extra feature's own switch (prefs "feat_drive", "feat_ssh", "feat_quick"); on unless switched off.</summary>
		public static bool FeatureEnabled(string name)
		{
			return WirePodConfig.Prefs.GetString("feat_" + name, "1") == "1";
		}

		public static void SaveServer(string server, string resolvedIp)
		{
			WirePodConfig.Prefs.Edit().PutString(WirePodConfig.kServerKey, server).PutString(WirePodConfig.kServerIpKey, resolvedIp).Apply();
			DAS.Event("WirePod.WirePodConfig.SaveServer", string.Format("server: {0} ip: {1}", server, resolvedIp));
		}

		/// <summary>
		/// Replaces the string.Concat that built "https://session-certs.token.global.anki-services.com/vic/" + esn.
		/// So that we can get the robot name for the session cert
		/// </summary>
		public static string SessionCertUrl(string ankiCertAddress, string esn, RobotCommunicationConnectionModule module)
		{
			string text = (esn ?? string.Empty).Trim().ToLowerInvariant();
			string text2 = null;
			try
			{
				if (module != null)
				{
					text2 = WirePodConfig.CleanRobotName(module.LastConnectedRobotName);
					if (string.IsNullOrEmpty(text2))
					{
						text2 = WirePodConfig.CleanRobotName(module.LastSelectedRobotName);
					}
				}
			}
			catch (Exception ex)
			{
				DAS.Warn("WirePod.WirePodConfig.SessionCertUrl", "couldn't read robot name: " + ex.Message);
			}
			string text3 = WirePodConfig.WebBaseUrl + "/v1/session_cert/" + (string.IsNullOrEmpty(text2) ? text : (text2 + "_" + text));
			DAS.Event("WirePod.WirePodConfig.SessionCertUrl", text3);
			return text3;
		}

		/// <summary>
		/// Make the cert download work
		/// </summary>
		public static Task<string> FetchCert(object appHttpService, string url)
		{
			return Task.Run<string>(delegate
			{
				try
				{
					using (HttpClient httpClient = new HttpClient(new AndroidClientHandler()))
					{
						httpClient.Timeout = TimeSpan.FromSeconds(15.0);
						// Signed in: the server gives this robot to the account (first come), and refuses someone else's.
						if (!string.IsNullOrEmpty(WirePodConfig.SessionToken))
						{
							httpClient.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + WirePodConfig.SessionToken);
						}
						string result = httpClient.GetStringAsync(url).Result;
						DAS.Event("WirePod.WirePodConfig.FetchCert", string.Format("{0}: {1} bytes", url, result.Length));
						return result;
					}
				}
				catch (AggregateException ex)
				{
					Exception ex2 = ex.InnerException ?? ex;
					DAS.Warn("WirePod.WirePodConfig.FetchCert", url + ": " + ex2.Message);
					throw ex2;
				}
			});
		}

		private static string CleanRobotName(string name)
		{
			if (string.IsNullOrWhiteSpace(name))
			{
				return null;
			}
			string text = string.Empty;
			foreach (char c in name.Trim())
			{
				if (char.IsLetterOrDigit(c) || c == '-')
				{
					text += c;
				}
				else if (c == ' ')
				{
					text += '-';
				}
			}
			return (text.Length == 0) ? null : text;
		}

		/// <summary>
		/// Replaces RobotCommunicationService.OtaUrl: the firmware Vector downloads (himself) when the app updates him,
		/// e.g. from recovery mode. Escape-pod firmware 2.0.1.6086ep from the Internet Archive, over plain HTTP: the
		/// robot's updater may not have a correct clock or current certificates for HTTPS.
		/// </summary>
		public static string OtaUrl(string ankiOtaUrl)
		{
			// The community server hosts your CFW's updates at /vic/, also over plain HTTP (ota_http_port, 8088): a robot
			// in recovery runs old firmware that may not manage modern HTTPS.
			return "http://" + WirePodConfig.GetHost(WirePodConfig.Server) + ":" + WirePodConfig.kOtaHttpPort + "/vic/full/latest.ota";
		}

		/// <summary>
		/// Accepts "host", "host:port" or a pasted URL such as "http://host:8080/" and returns "host:port".
		/// </summary>
		public static bool TryNormalizeServer(string input, out string server)
		{
			server = null;
			if (string.IsNullOrWhiteSpace(input))
			{
				return false;
			}
			string text = input.Trim();
			if (!text.Contains("://"))
			{
				text = "https://" + text;
			}
			Uri uri;
			if (!Uri.TryCreate(text, UriKind.Absolute, out uri) || string.IsNullOrEmpty(uri.Host))
			{
				return false;
			}
			int num = (uri.IsDefaultPort && !WirePodConfig.HasExplicitPort(input)) ? WirePodConfig.kDefaultWebPort : uri.Port;
			server = uri.Host + ":" + num;
			return true;
		}

		public static string GetHost(string server)
		{
			int num = server.LastIndexOf(':');
			if (num <= 0)
			{
				return server;
			}
			return server.Substring(0, num);
		}

		public static int GetPort(string server)
		{
			int num = server.LastIndexOf(':');
			int num2;
			if (num <= 0 || !int.TryParse(server.Substring(num + 1), out num2))
			{
				return WirePodConfig.kDefaultWebPort;
			}
			return num2;
		}

		/// <summary>Returns the first IPv4 address for host, or null if the phone can't resolve it.</summary>
		public static string ResolveIPv4(string host)
		{
			IPAddress ipaddress;
			if (IPAddress.TryParse(host.Trim('[', ']'), out ipaddress))
			{
				return (ipaddress.AddressFamily == AddressFamily.InterNetwork) ? ipaddress.ToString() : null;
			}
			try
			{
				IPAddress ipaddress2 = Dns.GetHostAddresses(host).FirstOrDefault((IPAddress a) => a.AddressFamily == AddressFamily.InterNetwork);
				return (ipaddress2 != null) ? ipaddress2.ToString() : null;
			}
			catch (Exception ex)
			{
				DAS.Warn("WirePod.WirePodConfig.ResolveIPv4", host + ": " + ex.Message);
				return null;
			}
		}

		private static bool HasExplicitPort(string input)
		{
			string text = input.Trim();
			int num = text.IndexOf("://", StringComparison.Ordinal);
			if (num >= 0)
			{
				text = text.Substring(num + 3);
			}
			int num2 = text.IndexOf('/');
			if (num2 >= 0)
			{
				text = text.Substring(0, num2);
			}
			int num3 = text.LastIndexOf(':');
			return num3 > 0 && num3 > text.LastIndexOf(']');
		}

		internal static ISharedPreferences Prefs
		{
			get
			{
				return Application.Context.GetSharedPreferences(WirePodConfig.kPrefsName, FileCreationMode.Private);
			}
		}

		public const string kDefaultHost = "server.vectortech.dev";

		/// <summary>The VTCS app always talks to VTCS (kDefaultHost): no server prompt, and any address saved by an
		/// older version is ignored.</summary>
		public const bool kFixedServer = true;

		/// <summary>VTCS's accounts API port (the Pi's port 443 is wire-pod's voice server).</summary>
		public const int kFixedPort = 8443;

		public const int kDefaultWebPort = 443;


		/// <summary>The community server's plain-HTTP port for OS updates (its ota_http_port setting).</summary>
		private const int kOtaHttpPort = 8088;

		private const string kPrefsName = "wirepod";

		private const string kServerKey = "server";


		/// <summary>The session token from sign-in; the extra features send it to the server (Authorization: Bearer).</summary>
		public const string kSessionTokenKey = "session_token";

		public static string SessionToken
		{
			get
			{
				return WirePodConfig.Prefs.GetString(WirePodConfig.kSessionTokenKey, string.Empty);
			}
		}

		private const string kServerIpKey = "server_ip";
	}
}
