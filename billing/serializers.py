from rest_framework import serializers

from .models import Customer, Invoice, InvoiceItem, Payment, Product, SalesReturn, SalesReturnItem


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "email", "created_at"]
        read_only_fields = ["created_at"]


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "sku", "name", "unit_price", "stock"]


class InvoiceItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = InvoiceItem
        fields = ["id", "product", "product_name", "quantity", "unit_price", "line_total"]


class InvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    items = InvoiceItemSerializer(many=True, read_only=True)
    balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = "__all__"


class InvoiceLineInputSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class InvoiceCreateSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    due_date = serializers.DateField()
    items = InvoiceLineInputSerializer(many=True, allow_empty=False)

    def validate_customer_id(self, value):
        business = self.context["request"].user.profile.business
        if not Customer.objects.filter(pk=value, business=business).exists():
            raise serializers.ValidationError("Customer not found.")
        return value

    def validate_items(self, value):
        product_ids = [row["product_id"] for row in value]
        if len(product_ids) != len(set(product_ids)):
            raise serializers.ValidationError("Each product can appear only once.")
        return value


class PaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    method = serializers.ChoiceField(choices=Payment.Method.choices)
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100)


class ReturnLineInputSerializer(serializers.Serializer):
    invoice_item_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class ReturnCreateSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)
    items = ReturnLineInputSerializer(many=True, allow_empty=False)

    def validate_items(self, value):
        item_ids = [row["invoice_item_id"] for row in value]
        if len(item_ids) != len(set(item_ids)):
            raise serializers.ValidationError("Each invoice line can appear only once.")
        return value


class SalesReturnItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True)

    class Meta:
        model = SalesReturnItem
        fields = [
            "id",
            "invoice_item",
            "product",
            "product_sku",
            "product_name",
            "quantity",
            "unit_price",
            "line_total",
        ]


class SalesReturnSerializer(serializers.ModelSerializer):
    items = SalesReturnItemSerializer(many=True, read_only=True)
    document_number = serializers.SerializerMethodField()

    class Meta:
        model = SalesReturn
        fields = [
            "id",
            "document_number",
            "number",
            "invoice",
            "reason",
            "subtotal",
            "tax",
            "total",
            "refund_amount",
            "created_by",
            "created_at",
            "items",
        ]
        read_only_fields = fields

    def get_document_number(self, obj):
        return f"RET-{obj.number}"
