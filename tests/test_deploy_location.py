import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from deploy import config as deploy_config
from deploy import geo
from deploy import git as deploy_git
from deploy.Windows import config as windows_config
from deploy.Windows import git as windows_git


class TestIp9Location(unittest.TestCase):
    @patch('deploy.geo.requests.get')
    def test_returns_lowercase_country_code(self, get):
        response = get.return_value
        response.json.return_value = {'data': {'country_code': 'CN'}}

        self.assertEqual(geo.get_country_code(), 'cn')

        get.assert_called_once_with(
            geo.IP9_LOCATION_URL,
            timeout=5,
            headers={'User-Agent': 'AzurPilot'},
        )
        response.raise_for_status.assert_called_once_with()

    @patch('deploy.geo.requests.get')
    def test_invalid_response_returns_none(self, get):
        response = get.return_value
        response.json.return_value = {'data': {}}

        self.assertIsNone(geo.get_country_code())


class TestDeployLocation(unittest.TestCase):
    def make_config(self, config_module, repository=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        template = root / 'template.yaml'
        file = root / 'deploy.yaml'
        text = f'Repository: {repository or config_module.GITHUB_REPOSITORY}\nBranch: master\n'
        template.write_text(text)
        file.write_text(text)
        template_patch = (patch.object(config_module, 'get_deploy_template', return_value=str(template))
                          if config_module is deploy_config
                          else patch.object(config_module, 'DEPLOY_TEMPLATE', str(template)))
        with template_patch, patch.object(config_module.DeployConfig, 'show_config'):
            return config_module.DeployConfig(str(file))

    def test_china_uses_cdn_and_gitcode_fallback(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__), patch.object(
                config_module, 'get_country_code', return_value='cn'
            ) as get_country_code:
                config = self.make_config(config_module)

                self.assertEqual(config.config['Repository'], config_module.GIT_OVER_CDN_REPOSITORY)
                self.assertTrue(config.GitOverCdn)
                self.assertIn(config.Repository, config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES)
                get_country_code.assert_called_once_with()

    def test_fallback_repositories_include_cnb_and_gitcode(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                self.assertIn('https://cnb.cool/AzurPilot/AzurPilot', config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES)
                self.assertIn('https://gitcode.com/ddl2/AzurPilot', config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES)

    def test_cnb_alias_and_repository(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                config = self.make_config(config_module, 'cnb')
                config.config_redirect()
                self.assertEqual(config.Repository, config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES[0])

    def test_gitcode_alias_and_repository(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__):
                config = self.make_config(config_module, 'gitcode')
                config.config_redirect()
                self.assertEqual(config.Repository, config_module.GIT_OVER_CDN_FALLBACK_REPOSITORIES[1])

    def test_non_china_keeps_github_and_only_checks_once(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__), patch.object(
                config_module, 'get_country_code', return_value='us'
            ) as get_country_code:
                config = self.make_config(config_module)

                config.config_redirect()
                config.config_redirect()

                self.assertEqual(config.config['Repository'], config_module.GITHUB_REPOSITORY)
                self.assertFalse(config.GitOverCdn)
                self.assertEqual(config.Repository, config_module.GITHUB_REPOSITORY)
                get_country_code.assert_called_once_with()

    def test_failed_lookup_keeps_github(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__), patch.object(
                config_module, 'get_country_code', return_value=None
            ):
                config = self.make_config(config_module)

                config.config_redirect()

                self.assertEqual(config.config['Repository'], config_module.GITHUB_REPOSITORY)
                self.assertFalse(config.GitOverCdn)

    def test_custom_repository_does_not_query_location(self):
        for config_module in (deploy_config, windows_config):
            with self.subTest(config_module=config_module.__name__), patch.object(
                config_module, 'get_country_code'
            ) as get_country_code:
                config = self.make_config(config_module, 'https://github.com/example/custom')

                config.config_redirect()

                get_country_code.assert_not_called()

    def test_peer_fallback_repository(self):
        for git_mod in (deploy_git, windows_git):
            with self.subTest(git_module=git_mod.__name__):
                self.assertEqual(
                    git_mod.peer_fallback_repository('https://cnb.cool/AzurPilot/AzurPilot'),
                    'https://gitcode.com/ddl2/AzurPilot',
                )
                self.assertEqual(
                    git_mod.peer_fallback_repository('https://cnb.cool/AzurPilot/AzurPilot.git'),
                    'https://gitcode.com/ddl2/AzurPilot',
                )
                self.assertEqual(
                    git_mod.peer_fallback_repository('https://gitcode.com/ddl2/AzurPilot'),
                    'https://cnb.cool/AzurPilot/AzurPilot',
                )
                self.assertEqual(
                    git_mod.peer_fallback_repository('https://gitcode.com/ddl2/AzurPilot.git'),
                    'https://cnb.cool/AzurPilot/AzurPilot',
                )
                self.assertIsNone(git_mod.peer_fallback_repository('https://github.com/wess09/AzurPilot'))
                self.assertIsNone(git_mod.peer_fallback_repository(''))


if __name__ == '__main__':
    unittest.main()
