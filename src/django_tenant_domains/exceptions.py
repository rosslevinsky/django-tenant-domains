class TenantNotSet(Exception):
    """A tenant-scoped operation ran with no current tenant and outside no_tenant()."""


class TenantMismatch(Exception):
    """An operation would read or write across tenants."""


class TenantAsyncViewError(Exception):
    """An async view handled a tenant's request with row-level security on, without opting in."""


class TenantFull(Exception):
    """Adding someone would take the tenant past its max_users."""


class InvitationInvalid(Exception):
    """The invitation is unknown, used, expired, or addressed to someone else."""


class JoinRequestDecided(Exception):
    """Someone else has already approved or declined this join request."""


class AlreadyMember(InvitationInvalid):
    """The invitation is for someone who is already an active member of its tenant."""
