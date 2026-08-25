from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    """Project-wide pagination.

    ?page=N&page_size=M  (page_size capped at max_page_size)

    Response envelope:
        {count, total_pages, current_page, page_size, next, previous, results}

    The default page size is inherited from settings.PAGE_SIZE (env: PAGE_SIZE,
    default 30). Clients may override per-request with ?page_size=, up to
    max_page_size.
    """

    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "total_pages": self.page.paginator.num_pages,
                "current_page": self.page.number,
                "page_size": self.get_page_size(self.request),
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "results": data,
            }
        )
