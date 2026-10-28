from andy_trader.env import load_env_file


def test_polymarket_credentials_are_process_only(tmp_path):
    names = ('POLYMARKET_PRIVATE_KEY', 'POLYMARKET_FUNDER_ADDRESS', 'POLYMARKET_SIGNATURE_TYPE')
    path = tmp_path / '.env'
    path.write_text('\n'.join(f'{name}=from-file' for name in names) + '\nORDINARY=value')
    env = {}
    load_env_file(path, env)
    assert env == {'ORDINARY': 'value'}
    env.update({name: 'from-process' for name in names})
    load_env_file(path, env)
    assert all(env[name] == 'from-process' for name in names)
