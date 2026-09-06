from django.urls import path
from rest_framework.routers import DefaultRouter
from . import views
from apps.accounts.clerk import ClerkSyncView, ClerkWebhookView
from apps.accounts.firebase import FirebaseSyncView

router = DefaultRouter()
router.register(r"products", views.MedicationViewSet, basename="product")
router.register(r"variants", views.MedicationVariantViewSet, basename="variant")
router.register(r"categories", views.CategoryViewSet, basename="category")
router.register(r"manufacturers", views.ManufacturerViewSet, basename="manufacturer")
router.register(r"customers", views.CustomerViewSet, basename="customer")
router.register(r"debts", views.DebtViewSet, basename="debt")
router.register(r"sales", views.SaleViewSet, basename="sale")
# Owner-managed staff (accounts.User), tenant-scoped by hand in the viewset.
router.register(r"staff", views.StaffViewSet, basename="staff")
router.register(r"purchase-orders", views.PurchaseOrderViewSet, basename="purchase-order")
router.register(r"orders", views.OrderViewSet, basename="order")
# <scaffold:routes>
urlpatterns = router.urls + [
    # Svix delivers Clerk signups here. Verified against the raw body.
    path("clerk/webhook/", ClerkWebhookView.as_view(), name="clerk-webhook"),
    # Called once on sign-in, so a customer exists without a webhook tunnel.
    path("clerk/sync/", ClerkSyncView.as_view(), name="clerk-sync"),
    # Same contract as clerk/sync/, for the native app. Called once per
    # sign-in so the Customer row exists before the first /shop/me/.
    path("firebase/sync/", FirebaseSyncView.as_view(), name="firebase-sync"),
    path(
        "store/quick-groups/",
        views.QuickGroupsView.as_view(),
        name="store-quick-groups",
    ),
    path("pos/cart-state/", views.PosCartStateView.as_view(), name="pos-cart-state"),
    path("public/price-check/", views.PublicPriceCheckView.as_view(), name="price-check"),
    path("public/scan-log/", views.PublicScanLogView.as_view(), name="scan-log"),
    path(
        "public/product-qr/",
        views.PublicProductQrView.as_view(),
        name="public-product-qr",
    ),
    path("public/stats/", views.PublicStatsView.as_view(), name="public-stats"),
    # The customer app reads its menu here — see PublicMenuView.
    path("public/menu/", views.PublicMenuView.as_view(), name="public-menu"),
    # The signed-in customer's own points, tier and history. Clerk-authed —
    # this is the shop app's equivalent of /auth/me/, and the reason the home
    # screen no longer ships hardcoded numbers.
    path("shop/me/", views.ShopMeView.as_view(), name="shop-me"),
    # The customer places orders here and reads their own history back.
    path("shop/orders/", views.ShopOrdersView.as_view(), name="shop-orders"),
    path(
        "shop/orders/<int:pk>/cancel/",
        views.ShopOrderCancelView.as_view(),
        name="shop-order-cancel",
    ),
    # This phone can receive push. Re-called on every launch: FCM tokens rotate.
    path("shop/devices/", views.ShopDeviceView.as_view(), name="shop-devices"),
    # One-tap reorder of what they always get.
    path("shop/usual/", views.ShopUsualView.as_view(), name="shop-usual"),
    # The durable half of notifications — works when push does not.
    path(
        "shop/notifications/",
        views.ShopNotificationsView.as_view(),
        name="shop-notifications",
    ),
    path("public/branding/", views.PublicBrandingView.as_view(), name="public-branding"),
    path("public/branding/icon/", views.PublicBrandingIconView.as_view(), name="public-branding-icon"),
    path("import/hesabate/products/", views.HesabateImportProductsView.as_view(), name="import-products"),
    path("import/hesabate/sales/", views.HesabateImportSalesView.as_view(), name="import-sales"),
    # The café's own report — drinks, hours, the app, the loyalty scheme.
    path("reports/cafe/", views.ReportsCafeView.as_view(), name="reports-cafe"),
    path("reports/summary/", views.ReportsSummaryView.as_view(), name="reports-summary"),
    path("reports/teaser/", views.ReportsTeaserView.as_view(), name="reports-teaser"),
    path("reports/sales/summary/", views.SalesReportsSummaryView.as_view(), name="reports-sales-summary"),
    path("reports/sales/export/", views.SalesReportsExportView.as_view(), name="reports-sales-export"),
    path("reports/products/", views.ReportsProductsView.as_view(), name="reports-products"),
    path("reports/filtered-charts/", views.ReportsFilteredChartsView.as_view(), name="reports-filtered-charts"),
    path("audit/", views.AuditLogView.as_view(), name="audit-list"),
    path("audit/<int:pk>/undo/", views.AuditLogView.as_view(), name="audit-undo"),
    path("reports/top-products/", views.ReportsTopProductsView.as_view(), name="reports-top-products"),
    path("reports/export/", views.ReportsExportView.as_view(), name="reports-export"),
    path("reports/restock-quota/", views.ReportsRestockQuotaView.as_view(), name="reports-restock-quota"),
    path("reports/scans/", views.ReportsScansView.as_view(), name="reports-scans"),
    path("qr/price-page/", views.PricePageQrView.as_view(), name="qr-price-page"),
    path("store/branding/", views.PharmacyBrandingView.as_view(), name="store-branding"),
]
