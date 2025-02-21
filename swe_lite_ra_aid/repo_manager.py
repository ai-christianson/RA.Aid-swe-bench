import os
import io
import tarfile
import hashlib
import tempfile
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Tuple
from git import Repo

from .logger import logger

class RepoManager:
    def __init__(self, cache_root: Path):
        self.init(cache_root)

    def init(self, cache_root: Path):
        """
        Initialize RepoManager with root directory for cached repos.
        Args:
            cache_root: Root directory where cached repositories will be stored.
                        Should be an absolute path to project_root/repos/
        """
        self.cache_root = Path(cache_root).resolve()
        logger.info(f"Initializing RepoManager with cache root: {self.cache_root}")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        
        # Create venvs directory for storing virtual environments
        self.venvs_root = self.cache_root / "venvs"
        self.venvs_root.mkdir(parents=True, exist_ok=True)
        logger.info(f"Virtual environments will be stored in: {self.venvs_root}")

        self.ra_aid_version = self._detect_ra_aid_version()
        logger.debug(f"ra_aid_version={self.ra_aid_version}")

        # Check for S3 configuration (including Backblaze B2 in S3-compatible mode)
        self.s3_enabled = bool(os.getenv('S3_BUCKET_NAME'))
        if self.s3_enabled:
            self.s3_bucket = os.getenv('S3_BUCKET_NAME')
            self.s3_endpoint = os.getenv('S3_ENDPOINT_URL')
            prefix = os.getenv('S3_PATH_PREFIX')
            if not prefix:
                prefix = "ra-aid-eval-repo-cache/"
            elif prefix and not prefix.endswith('/'):
                prefix += '/'
            self.s3_prefix = prefix
            logger.info(f"S3 caching enabled. Bucket: {self.s3_bucket}, Prefix: {self.s3_prefix}, Endpoint: {self.s3_endpoint}")
        else:
            logger.info("S3 caching not enabled; using local file caching.")

    def get_venv_path(self, repo_name: str, setup_commit: str) -> Path:
        """Get path to cached virtual environment directory."""
        safe_name = repo_name.replace("/", "_")
        venv_dir = self.venvs_root / f"{safe_name}_{setup_commit}"
        return venv_dir

    def _detect_ra_aid_version(self) -> str:
        """Detect installed ra-aid version."""
        from .config import DEFAULT_RA_AID_VERSION

        try:
            result = subprocess.run(
                ["ra-aid", "--version"], capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except Exception as e:
            logging.warning(f"Failed to detect ra-aid version: {e}")
            return DEFAULT_RA_AID_VERSION

    def get_cached_repo_path(self, repo_name: str) -> Path:
        """Get path where cached repo should be stored locally."""
        logger.debug(f"Getting cached path for repo: {repo_name}")

        # Extract owner/repo part from full URL if needed
        if "github.com/" in repo_name:
            repo_name = repo_name.split("github.com/")[-1]
            logger.debug(f"Extracted from URL: {repo_name}")

        # Handle both https and git protocols
        repo_name = repo_name.replace("https://", "").replace("git://", "")

        # Convert owner/repo to owner__repo format
        safe_name = repo_name.replace("/", "__")
        cache_path = self.cache_root / safe_name

        logger.debug(f"Converted to safe name: {safe_name}")
        return cache_path

    # --- S3 Caching Helper Methods ---
    def _repo_to_key(self, repo_url: str) -> str:
        """Generate an S3 object key for the given repository URL."""
        key = repo_url
        # For SSH URLs like git@github.com:owner/repo.git, replace ":" with "/"
        if key.startswith("git@"):
            key = key.replace("git@", "")
            key = key.replace(":", "/")
        # Remove scheme if present
        if "://" in key:
            key = key.split("://", 1)[1]
        # Remove trailing .git if present
        if key.endswith(".git"):
            key = key[:-4]
        key = key.rstrip('/')
        # Final key: prefix + normalized repo URL + extension
        return f"{self.s3_prefix}{key}.tar.zst"

    def is_cached_s3(self, repo_url: str) -> bool:
        """Check if repository archive exists in S3 using AWS CLI."""
        key = self._repo_to_key(repo_url)
        cmd = [
            "aws", "s3api", "head-object",
            "--bucket", self.s3_bucket,
            "--key", key
        ]
        if self.s3_endpoint:
            cmd.extend(["--endpoint-url", self.s3_endpoint])
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            return True
        else:
            stderr = result.stderr
            if "NotFound" in stderr or "404" in stderr:
                return False
            else:
                raise RuntimeError(f"Error checking S3 cache: {stderr}")

    def download_repo_from_s3(self, repo_url: str, dest_path: str) -> bool:
        """
        Download the repository archive from S3 using AWS CLI and extract it to dest_path.
        Returns True if successful, False if the cache is invalid.
        """
        key = self._repo_to_key(repo_url)
        if os.path.exists(dest_path):
            if os.listdir(dest_path):
                raise RuntimeError(f"Destination path {dest_path} is not empty.")
        else:
            os.makedirs(dest_path, exist_ok=True)
        
        # Retrieve metadata using aws s3api head-object
        head_cmd = [
            "aws", "s3api", "head-object",
            "--bucket", self.s3_bucket,
            "--key", key
        ]
        if self.s3_endpoint:
            head_cmd.extend(["--endpoint-url", self.s3_endpoint])
        head_result = subprocess.run(head_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if head_result.returncode != 0:
            stderr = head_result.stderr
            if "NotFound" in stderr or "404" in stderr:
                return False
            else:
                raise RuntimeError(f"Error getting head object: {stderr}")
        try:
            import json
            head_obj = json.loads(head_result.stdout)
            metadata = head_obj.get("Metadata", {})
            expected_sha = metadata.get('sha256')
        except Exception:
            expected_sha = None

        temp_fd, temp_archive_path = tempfile.mkstemp(suffix=".tar.zst")
        os.close(temp_fd)
        cp_cmd = [
            "aws", "s3", "cp",
            f"s3://{self.s3_bucket}/{key}",
            temp_archive_path
        ]
        if self.s3_endpoint:
            cp_cmd.extend(["--endpoint-url", self.s3_endpoint])
        cp_result = subprocess.run(cp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if cp_result.returncode != 0:
            stderr = cp_result.stderr
            if "NotFound" in stderr or "404" in stderr:
                return False
            else:
                raise RuntimeError(f"Error downloading S3 object: {stderr}")

        hasher = hashlib.sha256()
        with open(temp_archive_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                hasher.update(chunk)
        actual_sha = hasher.hexdigest()
        if expected_sha and expected_sha != actual_sha:
            logger.warning(f"Cache archive checksum mismatch (expected {expected_sha}, got {actual_sha}).")
            rm_cmd = [
                "aws", "s3", "rm",
                f"s3://{self.s3_bucket}/{key}"
            ]
            if self.s3_endpoint:
                rm_cmd.extend(["--endpoint-url", self.s3_endpoint])
            subprocess.run(rm_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.remove(temp_archive_path)
            return False

        try:
            import zstandard
        except ImportError:
            raise RuntimeError("Missing zstandard library for decompression.")
        dctx = zstandard.ZstdDecompressor()
        temp_extract_dir = dest_path + "_partial"
        os.makedirs(temp_extract_dir, exist_ok=True)
        try:
            with open(temp_archive_path, 'rb') as compressed_file:
                with dctx.stream_reader(compressed_file) as reader:
                    with tarfile.open(fileobj=reader, mode='r|') as tar:
                        tar.extractall(path=temp_extract_dir)
            if os.path.exists(dest_path):
                shutil.rmtree(dest_path)
            os.rename(temp_extract_dir, dest_path)
        except Exception as e:
            shutil.rmtree(temp_extract_dir, ignore_errors=True)
            raise e
        finally:
            os.remove(temp_archive_path)
        return True

    def _compress_repo(self, repo_dir: str, output_path: str) -> str:
        """Create a .tar.zst archive from the repository directory. Returns the SHA-256 hash of the archive."""
        try:
            import zstandard
        except ImportError:
            raise RuntimeError("Missing zstandard library for compression.")
        if os.path.exists(output_path):
            os.remove(output_path)
        hasher = hashlib.sha256()
        with open(output_path, 'wb') as out_f:
            cctx = zstandard.ZstdCompressor(level=3)
            with cctx.stream_writer(out_f) as compressor:
                with tarfile.open(mode="w|", fileobj=compressor) as tar:
                    tar.add(repo_dir, arcname="")
        with open(output_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                hasher.update(chunk)
        archive_sha = hasher.hexdigest()
        return archive_sha

    def upload_repo_to_s3(self, repo_url: str, repo_dir: str):
        """
        Compress the repository directory and upload it to S3 cache using AWS CLI.
        """
        key = self._repo_to_key(repo_url)
        temp_archive = tempfile.NamedTemporaryFile(suffix=".tar.zst", delete=False)
        archive_path = temp_archive.name
        temp_archive.close()
        try:
            sha256_hex = self._compress_repo(repo_dir, archive_path)
            archive_size = os.path.getsize(archive_path)
            if archive_size <= 5 * 1024 * 1024 * 1024:
                # For small files, use put-object with metadata
                put_cmd = [
                    "aws", "s3api", "put-object",
                    "--bucket", self.s3_bucket,
                    "--key", key,
                    "--body", archive_path,
                    "--metadata", f"sha256={sha256_hex}"
                ]
                if self.s3_endpoint:
                    put_cmd.extend(["--endpoint-url", self.s3_endpoint])
                put_result = subprocess.run(put_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if put_result.returncode != 0:
                    stderr = put_result.stderr
                    if any(err in stderr for err in ["PreconditionFailed", "412", "ConditionalCheckFailed", "ConditionalRequestConflict", "409"]):
                        logger.info("Another process has uploaded the cache concurrently. Using existing cache.")
                    else:
                        raise RuntimeError(f"Error uploading S3 object: {stderr}")
                else:
                    logger.info(f"Uploaded cache to s3://{self.s3_bucket}/{key}")
            else:
                # For large files, use aws s3 cp and then update metadata via copy-object
                cp_cmd = [
                    "aws", "s3", "cp",
                    archive_path,
                    f"s3://{self.s3_bucket}/{key}"
                ]
                if self.s3_endpoint:
                    cp_cmd.extend(["--endpoint-url", self.s3_endpoint])
                cp_result = subprocess.run(cp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if cp_result.returncode != 0:
                    raise RuntimeError(f"Error uploading S3 object: {cp_result.stderr}")
                copy_cmd = [
                    "aws", "s3api", "copy-object",
                    "--bucket", self.s3_bucket,
                    "--copy-source", f"{self.s3_bucket}/{key}",
                    "--key", key,
                    "--metadata", f"sha256={sha256_hex}",
                    "--metadata-directive", "REPLACE"
                ]
                if self.s3_endpoint:
                    copy_cmd.extend(["--endpoint-url", self.s3_endpoint])
                copy_result = subprocess.run(copy_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if copy_result.returncode != 0:
                    raise RuntimeError(f"Error setting metadata on S3 object: {copy_result.stderr}")
                logger.info(f"Uploaded (multipart) cache to s3://{self.s3_bucket}/{key}")
        finally:
            try:
                os.remove(archive_path)
            except OSError:
                pass

    def _clone_and_cache(self, repo_url: str, cache_path: Path, setup_commit: str, version: str):
        """Helper to clone repo from git and upload to S3 if enabled."""
        cache_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Cloning {repo_url} to cache at {cache_path}")
        repo = Repo.clone_from(repo_url, str(cache_path))
        repo.git.checkout(setup_commit)
        if self.s3_enabled:
            try:
                self.upload_repo_to_s3(repo_url, str(cache_path))
            except Exception as e:
                logger.warning(f"Failed to upload repo cache to S3: {e}")

    def ensure_venv(self, repo_name: str, setup_commit: str, version: str, cache_path: Path) -> None:
        """
        Ensure virtual environment exists and is set up with dependencies.
        """
        venv_path = self.get_venv_path(repo_name, setup_commit)
        logger.debug("ENSURE_VENV:")
        logger.debug(f"repo_name: {repo_name}")
        logger.debug(f"setup_commit: {setup_commit}")
        logger.debug(f"version: {version}")
        logger.debug(f"cache_path: {cache_path}")
        logger.debug(f"venv_path: {venv_path}")

        if not (venv_path / ".venv").exists():
            logger.debug("\nSetting up new virtual environment:")
            logger.debug(f"venv_path: {venv_path}")
            venv_path.mkdir(parents=True, exist_ok=True)
            
            logger.debug("Copying repo contents to venv directory...")
            for item in Path(cache_path).iterdir():
                if item.name != ".git":
                    dest = venv_path / item.name
                    logger.debug(f"Copying {item} -> {dest}")
                    if item.is_dir():
                        shutil.copytree(item, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, dest)
            
            from .uv_utils import setup_venv_and_deps
            logger.debug("Calling setup_venv_and_deps...")
            setup_venv_and_deps(venv_path, repo_name, version, force_venv=True)
        else:
            logger.info(f"Using cached virtual environment at {venv_path}")

    def ensure_base_repo(self, repo_url: str, setup_commit: str, version: str) -> Tuple[Repo, Path]:
        """
        Ensure base repository exists in cache.
        Args:
            repo_url: GitHub repository URL
            setup_commit: Commit hash for environment setup
            version: Repository version for Python version detection
        Returns:
            Tuple of (Repo object, Path to cached repo)
        """
        logger.info(f"Ensuring base repo exists for URL: {repo_url}")
        logger.debug(f"Setup commit: {setup_commit}")

        repo_name = repo_url.split("github.com/")[-1]
        logger.debug(f"Extracted repo name: {repo_name}")

        cache_path = self.get_cached_repo_path(repo_name)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if self.s3_enabled:
            if self.is_cached_s3(repo_url):
                logger.info(f"Using S3 cached repo for {repo_url}")
                try:
                    downloaded = self.download_repo_from_s3(repo_url, str(cache_path))
                    if not downloaded:
                        raise Exception("Downloaded cache invalid")
                except Exception as e:
                    logger.warning(f"S3 cache download failed: {e}")
                    if os.path.exists(cache_path):
                        shutil.rmtree(cache_path)
                    self._clone_and_cache(repo_url, cache_path, setup_commit, version)
            else:
                self._clone_and_cache(repo_url, cache_path, setup_commit, version)
        else:
            if os.path.exists(cache_path):
                logger.info(f"Using cached repo at {cache_path}")
                try:
                    repo = Repo(cache_path)
                    repo.git.rev_parse("--git-dir")
                except Exception as e:
                    logger.warning(f"Cached repository is invalid: {e}")
                    logger.info("Removing corrupted cache and trying fresh clone")
                    shutil.rmtree(cache_path)
                    self._clone_and_cache(repo_url, cache_path, setup_commit, version)
            else:
                self._clone_and_cache(repo_url, cache_path, setup_commit, version)

        repo = Repo(cache_path)
        repo.git.checkout(setup_commit)
        self.ensure_venv(repo_name, setup_commit, version, cache_path)
        return repo, cache_path

    def create_venv_symlink(self, base_repo: Repo, worktree_path: Path, base_commit: str) -> Path:
        """
        Create symlink to cached virtual environment in worktree.
        Args:
            base_repo: Base repository object
            worktree_path: Path to worktree directory
            base_commit: Commit hash being used
        Returns:
            Path to the virtual environment that was linked
        """
        repo_name = Path(base_repo.working_dir).name.replace("__", "/")
        venv_path = self.get_venv_path(repo_name, base_commit) / ".venv"
        logger.debug(f"Linking to cached venv at: {venv_path}")
        worktree_venv = worktree_path / ".venv"

        try:
            os.symlink(venv_path, worktree_venv)
        except OSError as e:
            logger.warning(f"Failed to create symlink to cached venv: {e}")
            raise

        return venv_path

    def create_worktree(self, base_repo: Repo, base_commit: str, setup_commit: str) -> Tuple[Path, Path]:
        """
        Create new worktree for given commit with symlinked .venv.
        Args:
            base_repo: Base repository object
            base_commit: Commit hash to checkout
            setup_commit: Commit hash for environment setup
        Returns:
            Tuple of (worktree path, symlink target path)
        """
        worktree_name = f"worktree-{base_commit}-{tempfile.mktemp(dir='').split(os.sep)[-1]}"
        worktree_path = Path(base_repo.working_dir).parent / worktree_name

        base_repo.git.worktree("add", str(worktree_path), base_commit)
        venv_path = self.create_venv_symlink(base_repo, worktree_path, setup_commit)
        return worktree_path, venv_path

    def cleanup_worktree(self, repo: Repo, worktree_path: Path):
        """
        Remove worktree and its directory.
        Args:
            repo: Repository object
            worktree_path: Path to worktree to remove
        """
        try:
            shutil.rmtree(worktree_path)
        except Exception as e:
            logger.error(f"Error removing worktree directory: {e}")
