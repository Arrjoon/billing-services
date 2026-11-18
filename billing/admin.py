from django.contrib import admin

from .models import (
    Business,
    Customer,
    Invoice,
    InvoiceItem,
    Payment,
    Product,
    SalesReturn,
    SalesReturnItem,
    StaffProfile,
)


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "business", "customer", "status", "total", "amount_paid", "due_date")
    list_filter = ("status", "business")
    inlines = [InvoiceItemInline]


class SalesReturnItemInline(admin.TabularInline):
    model = SalesReturnItem
    extra = 0
    readonly_fields = ("invoice_item", "product", "quantity", "unit_price", "line_total")


@admin.register(SalesReturn)
class SalesReturnAdmin(admin.ModelAdmin):
    list_display = ("number", "business", "invoice", "total", "refund_amount", "created_at")
    list_filter = ("business",)
    inlines = [SalesReturnItemInline]
    readonly_fields = (
        "business",
        "invoice",
        "number",
        "subtotal",
        "tax",
        "total",
        "refund_amount",
        "created_by",
        "created_at",
    )


admin.site.register(Business)
admin.site.register(StaffProfile)
admin.site.register(Customer)
admin.site.register(Product)
admin.site.register(Payment)
