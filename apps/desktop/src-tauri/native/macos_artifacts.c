/* SDK-bound stable-snapshot operations. Only generated, fd-identified objects. */
#include <sys/acl.h>
#include <sys/clonefile.h>
#include <sys/stat.h>
#include <copyfile.h>
#include <unistd.h>
#include <errno.h>
#include <stdint.h>
#include <sys/xattr.h>

int tr_private_fd(int fd) {
    struct stat st;
    if (fstat(fd, &st) || geteuid() == 0 || geteuid() != getuid() ||
        st.st_uid != geteuid() || (st.st_mode & 0077)) { errno = EPERM; return -1; }
    errno = 0;
    acl_t acl = acl_get_fd_np(fd, ACL_TYPE_EXTENDED);
    /* macOS reports ENOENT when this open, already-fstatted descriptor has no
       extended ACL. Treat that as an empty ACL; keep all other errors closed. */
    if (!acl) return errno == ENOENT ? 0 : -1;
    acl_entry_t entry;
    int result = acl_get_entry(acl, ACL_FIRST_ENTRY, &entry);
    int saved_errno = errno;
    acl_free(acl);
    /* Darwin returns -1 with EINVAL for an empty ACL. Verify with acl_valid
       instead via an entry count fetched from acl_to_text is unnecessary:
       acl_get_entry returns 0 on an entry, -1 when exhausted. */
    if (result == 0) { errno = EPERM; return -1; }
    if (saved_errno != EINVAL) { errno = saved_errno; return -1; }
    return 0;
}
int tr_seal_fd(int fd, uint32_t mode) {
    struct stat before, after;
    if (fstat(fd, &before) || fchmod(fd, mode) || fchflags(fd, UF_IMMUTABLE) || fstat(fd, &after)) return -1;
    if (before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
        !(after.st_flags & UF_IMMUTABLE) || (after.st_mode & 0777) != mode) { errno = EPERM; return -1; }
    return 0;
}
int tr_unseal_fd(int fd) {
    struct stat st;
    if (fstat(fd, &st)) return -1;
    return fchflags(fd, st.st_flags & ~UF_IMMUTABLE);
}
/* 1 cloned, 0 bounded fd-copy fallback, -1 failure; no arbitrary fallback. */
int tr_clone_fd(int source, int parent, const char *name) {
    if (fclonefileat(source, parent, name, CLONE_NOOWNERCOPY) == 0) return 1;
    return (errno == ENOTSUP || errno == EXDEV) ? 0 : -1;
}
int tr_copy_xattrs(int source, int dest) {
    return fcopyfile(source, dest, NULL, COPYFILE_XATTR);
}

/* Preserve an enclosing bundle's quarantine without copying ACLs/ownership. */
int tr_copy_quarantine(int source, int dest) {
    char value[4096];
    ssize_t size = fgetxattr(source, "com.apple.quarantine", value, sizeof(value), 0, 0);
    if (size < 0) return errno == ENOATTR ? 0 : -1;
    if (fsetxattr(dest, "com.apple.quarantine", value, (size_t)size, 0, 0)) return -1;
    return 1;
}

/* Ancestors may have a standard deny-delete ACL; never accept an allow ACL. */
int tr_parent_fd(int fd) {
    errno = 0;
    acl_t acl = acl_get_fd_np(fd, ACL_TYPE_EXTENDED);
    /* No ACL on this already-open ancestor is equivalent to an empty list. */
    if (!acl) return errno == ENOENT ? 0 : -1;
    acl_entry_t entry;
    int selector = ACL_FIRST_ENTRY;
    for (;;) {
        if (acl_get_entry(acl, selector, &entry) != 0) {
            int saved = errno;
            acl_free(acl);
            if (saved == EINVAL) return 0;
            errno = saved; return -1;
        }
        acl_tag_t tag;
        if (acl_get_tag_type(entry, &tag) != 0 || tag != ACL_EXTENDED_DENY) {
            acl_free(acl); errno = EPERM; return -1;
        }
        selector = ACL_NEXT_ENTRY;
    }
}
