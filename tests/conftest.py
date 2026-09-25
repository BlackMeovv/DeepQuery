import pytest

from deepquery.config import Settings
from deepquery.demo_data import build
from deepquery.tools.database import ReadOnlyDatabase


@pytest.fixture(scope="session")
def demo_db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "ecommerce.sqlite"
    build(path)
    return path


@pytest.fixture(scope="session")
def db(demo_db_path):
    return ReadOnlyDatabase(demo_db_path, timeout_seconds=5, max_rows=200)


@pytest.fixture()
def settings(demo_db_path, tmp_path):
    # _env_file=None：测试不读 .env，行为与 CI 一致；可写路径全部指向临时目录
    return Settings(
        _env_file=None,
        db_path=str(demo_db_path),
        memory_db_path=str(tmp_path / "memory.sqlite"),
        run_log_path=str(tmp_path / "runs.sqlite"),
        chart_out_dir=str(tmp_path / "charts"),
        web_dist=str(tmp_path / "no-dist"),  # 测试默认走内置后备页，不受本地构建产物影响
    )
