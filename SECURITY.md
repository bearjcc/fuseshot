# Security

Please report security problems privately through
[GitHub's advisory form](https://github.com/bearjcc/fuseshot/security/advisories/new)
rather than an issue. I'll reply within a week.

Things worth reporting: the review page serving files outside its folders, accepting
changes from another site, leaking the Immich key or review token, Fuseshot
writing to source folders, or Fuseshot changing Immich in any way other than adding
kept results when uploads are turned on (and never with them off).

The review page is meant for one person on their own machine or behind their own
proxy. It has no user accounts; the optional token is a single shared secret.
